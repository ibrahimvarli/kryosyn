import argparse
import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sys


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_paths(base: Path):
    return {
        "os": base / "config" / "os.json",
        "channels": base / "config" / "channels.json",
        "profiles": base / "config" / "profiles.json",
        "packages": base / "packages" / "groups.json",
        "policies": base / "security" / "policies.json",
        "pipeline": base / "build" / "pipeline.json"
    }


def build_plan(base: Path):
    paths = get_paths(base)
    plan = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "os": load_json(paths["os"]),
        "channels": load_json(paths["channels"]),
        "profiles": load_json(paths["profiles"]),
        "packages": load_json(paths["packages"]),
        "policies": load_json(paths["policies"]),
        "pipeline": load_json(paths["pipeline"])
    }
    return plan


def validate_plan(base: Path):
    paths = get_paths(base)
    errors = []
    for key, path in paths.items():
        if not path.exists():
            errors.append(f"eksik_dosya:{key}:{path.as_posix()}")
    if errors:
        return errors
    os_cfg = load_json(paths["os"])
    profiles_cfg = load_json(paths["profiles"])
    packages_cfg = load_json(paths["packages"])
    required_os_keys = ["name", "edition", "base", "architecture", "kernel", "desktop", "defaults"]
    for key in required_os_keys:
        if key not in os_cfg:
            errors.append(f"os_anahtar_eksik:{key}")
    if "profiles" not in profiles_cfg:
        errors.append("profiles_anahtar_eksik:profiles")
    groups = packages_cfg.get("groups", {})
    for profile in profiles_cfg.get("profiles", []):
        for group in profile.get("groups", []):
            if group not in groups:
                errors.append(f"paket_grup_eksik:{profile.get('id','?')}:{group}")
    return errors


def write_export(base: Path, plan):
    out_dir = base / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "build-plan.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
    return out_path


def resolve_packages(base: Path, profile_id: str):
    paths = get_paths(base)
    profiles_cfg = load_json(paths["profiles"])
    packages_cfg = load_json(paths["packages"])
    profiles = profiles_cfg.get("profiles", [])
    profile = None
    profile_id = profile_id.strip()
    for item in profiles:
        if item.get("id") == profile_id:
            profile = item
            break
    if profile is None:
        candidates = [item.get("id") for item in profiles if item.get("id")]
        prefix_matches = [pid for pid in candidates if pid.startswith(profile_id)]
        if len(prefix_matches) == 1:
            profile = next(item for item in profiles if item.get("id") == prefix_matches[0])
        else:
            available = ",".join(candidates)
            hint = ",".join(prefix_matches)
            if hint:
                raise ValueError(f"profil_bulunamadi:{profile_id}:oneriler:{hint}:mevcut:{available}")
            raise ValueError(f"profil_bulunamadi:{profile_id}:mevcut:{available}")
    groups = packages_cfg.get("groups", {})
    resolved = []
    for group in profile.get("groups", []):
        for pkg in groups.get(group, []):
            if pkg not in resolved:
                resolved.append(pkg)
    return resolved


def render_sysctl(policies: dict):
    items = policies.get("hardening", {}).get("sysctl", {})
    lines = []
    for key in sorted(items.keys()):
        value = items[key]
        lines.append(f"{key} = {value}")
    return "\n".join(lines) + ("\n" if lines else "")


def render_blacklist(policies: dict):
    modules = policies.get("hardening", {}).get("modules", {}).get("blacklist", [])
    lines = []
    for module in modules:
        lines.append(f"blacklist {module}")
    return "\n".join(lines) + ("\n" if lines else "")


def ensure_tools(tools):
    missing = []
    for tool in tools:
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


def write_live_build_config(base: Path, profile_id: str, work_dir: Path):
    paths = get_paths(base)
    os_cfg = load_json(paths["os"])
    policies = load_json(paths["policies"])
    packages = resolve_packages(base, profile_id)
    lb_dir = work_dir / "live-build"
    if lb_dir.exists():
        shutil.rmtree(lb_dir)
    lb_dir.mkdir(parents=True, exist_ok=True)
    (lb_dir / "config" / "package-lists").mkdir(parents=True, exist_ok=True)
    (lb_dir / "config" / "includes.chroot" / "etc" / "sysctl.d").mkdir(parents=True, exist_ok=True)
    (lb_dir / "config" / "includes.chroot" / "etc" / "modprobe.d").mkdir(parents=True, exist_ok=True)
    suite = os_cfg.get("suite", "stable")
    arch = os_cfg.get("architecture", "amd64")
    name = os_cfg.get("name", "Kryosyn OS")
    volume = "".join([c for c in name.upper() if c.isalnum()])[:16] or "KRYOSYN"
    package_path = lb_dir / "config" / "package-lists" / "kryosyn.list.chroot"
    package_path.write_text("\n".join(packages) + "\n", encoding="utf-8")
    sysctl_path = lb_dir / "config" / "includes.chroot" / "etc" / "sysctl.d" / "99-kryosyn.conf"
    sysctl_path.write_text(render_sysctl(policies), encoding="utf-8")
    blacklist_path = lb_dir / "config" / "includes.chroot" / "etc" / "modprobe.d" / "kryosyn-blacklist.conf"
    blacklist_path.write_text(render_blacklist(policies), encoding="utf-8")
    return {
        "dir": lb_dir,
        "suite": suite,
        "arch": arch,
        "name": name,
        "volume": volume
    }


def run_command(command, cwd):
    subprocess.run(command, cwd=cwd, check=True)


def build_iso(base: Path, profile_id: str, work_dir: Path, dry_run: bool):
    plan = build_plan(base)
    write_export(base, plan)
    config = write_live_build_config(base, profile_id, work_dir)
    lb_dir = config["dir"]
    if dry_run:
        return {"status": "dry-run", "workdir": lb_dir.as_posix()}
    if platform.system() != "Linux":
        return {"status": "linux-required", "workdir": lb_dir.as_posix()}
    missing = ensure_tools(["lb", "debootstrap", "xorriso", "mksquashfs"])
    if missing:
        raise RuntimeError("eksik_araclar:" + ",".join(missing))
    config_cmd = [
        "lb",
        "config",
        "--architecture",
        config["arch"],
        "--distribution",
        config["suite"],
        "--binary-images",
        "iso-hybrid",
        "--archive-areas",
        "main contrib non-free non-free-firmware",
        "--debian-installer",
        "live",
        "--iso-application",
        config["name"],
        "--iso-volume",
        config["volume"],
        "--checksums",
        "sha256"
    ]
    run_command(config_cmd, lb_dir)
    run_command(["lb", "build"], lb_dir)
    artifacts = base / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    iso_candidates = list(lb_dir.glob("*.iso"))
    if not iso_candidates:
        raise RuntimeError("iso_uretimi_basarisiz")
    output = artifacts / "kryosyn.iso"
    shutil.copy2(iso_candidates[0], output)
    return {"status": "built", "iso": output.as_posix()}


def main():
    parser = argparse.ArgumentParser(prog="kryosynctl")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("plan")
    subparsers.add_parser("validate")
    subparsers.add_parser("export")
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--target", choices=["iso"], default="iso")
    build_parser.add_argument("--profile", default="base")
    build_parser.add_argument("--work-dir", default=None)
    build_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1]
    command = args.command or "plan"
    if command == "validate":
        errors = validate_plan(base)
        if errors:
            print("\n".join(errors))
            return 1
        print("ok")
        return 0
    plan = build_plan(base)
    if command == "export":
        path = write_export(base, plan)
        print(path.as_posix())
        return 0
    if command == "build":
        errors = validate_plan(base)
        if errors:
            print("\n".join(errors))
            return 1
        work_dir = Path(args.work_dir) if args.work_dir else base / "build" / "work"
        result = build_iso(base, args.profile, work_dir, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
