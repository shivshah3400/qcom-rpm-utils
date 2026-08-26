#!/usr/bin/env python3
# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
"""
create_data_tar.py

Standalone utility to:
- Locate a .src.rpm via --path-to-rpms (file path or directory; if directory,
  the newest *.src.rpm is selected; default "." i.e. the current working
  directory)
- Find the sibling binary *.rpm files in the same directory that share the
  .src.rpm's version-release (its subpackages)
- Extract each binary rpm's raw payload into a shared staging directory
- Pack the staged content as <pkg>-<version>_<release>.<arch>.tar.gz, with
  the tarball's top-level entries matching the rpms' real paths (e.g.
  usr/..., etc/...)
- Place the tarball under <output-tar>/prebuilt_<distro>/ when --output-tar
  and --distro are provided; otherwise follow the fallback rules described
  in --output-tar help.

By default the script re-invokes itself inside a Docker container (as root)
so that it can always write to the output directory regardless of ownership
Pass --_in-docker internally (set automatically) to skip the re-invocation.
"""

import os
import sys
import argparse
import glob
import re
import tarfile
import shutil
import subprocess
import traceback
import json
import logging
import urllib.request
import urllib.parse
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("create_data_tar")

DEFAULT_BUILDER_IMAGE = "ghcr.io/qualcomm-linux/rpm-builder:centos10"
ARTIFACTORY_SEARCH_API = "https://qartifactory-edge.qualcomm.com/artifactory/api/search/artifact"
DOC_DIR_NAMES = ("usr/share/doc", "usr/share/man")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Generate a tarball of raw rpm contents from a .src.rpm's build."
    )
    parser.add_argument(
        "--path-to-rpms",
        required=False,
        default=".",
        help="Path to the .src.rpm file or a directory containing it (default: current directory, i.e. PWD). "
             "If a directory is provided, the newest *.src.rpm will be used."
    )
    parser.add_argument(
        "--output-tar",
        required=False,
        default="",
        help="Base output directory where the tarball will be placed. When --distro is provided, the tarball will be written to <output-tar>/prebuilt_<distro>/"
    )
    parser.add_argument(
        "--arch",
        required=False,
        default="",
        help="Architecture tag used in the output tarball name. Defaults to the ARCH tag of the binary rpms being packaged."
    )
    parser.add_argument(
        "--distro",
        required=False,
        default="",
        help="Target distro name (e.g., el10). If provided, tar will be placed under <output-tar>/prebuilt_<distro>/"
    )
    parser.add_argument(
        "--docker-image",
        required=False,
        default="",
        help=f"Docker image to use when running inside a container. Defaults to {DEFAULT_BUILDER_IMAGE}."
    )
    parser.add_argument("--_in-docker", dest="in_docker", action="store_true",
                        default=False, help=argparse.SUPPRESS)
    return parser.parse_args()


def rerun_in_docker(args, srpm_path: str) -> int:
    """
    Re-invoke this script inside a Docker container so it runs as root.
    This ensures write access to the output directory regardless of ownership.

    Mounts:
      <script_dir>      -> /scripts          (read-only: this script)
      <work_dir>        -> <work_dir>        (read-write: .src.rpm and .rpm files)
      <base_output_dir> -> <base_output_dir> (read-write: tarball destination)
      <invocation_cwd>  -> <invocation_cwd>  (read-write: PWD, the reference directory)

    work_dir and base_output_dir are mounted at their original absolute paths
    so all path arguments remain valid unchanged inside the container.
    If base_output_dir does not yet exist, Docker (running as root) creates it.

    Returns the container's exit code.
    """
    image_name = args.docker_image if args.docker_image else DEFAULT_BUILDER_IMAGE

    script_dir      = os.path.dirname(os.path.abspath(__file__))
    work_dir        = os.path.dirname(srpm_path)
    base_output_dir = os.path.abspath(args.output_tar) if args.output_tar else work_dir
    invocation_cwd  = os.path.abspath(os.getcwd())

    # Build a minimal set of data mounts (skip a path already covered by a
    # parent mount to avoid overlapping -v flags).
    candidates = sorted({work_dir, base_output_dir, invocation_cwd})
    data_mounts = []
    for d in candidates:
        if not any(d == r or d.startswith(r + os.sep) for r in data_mounts):
            data_mounts.append(d)

    docker_cmd = ['docker', 'run', '--rm',
                  '-v', f'{script_dir}:/scripts:ro,Z']
    for d in data_mounts:
        docker_cmd += ['-v', f'{d}:{d}:Z']

    docker_cmd += [image_name, 'python3', '/scripts/create_data_tar.py',
                   '--path-to-rpms', srpm_path]
    if args.arch:
        docker_cmd += ['--arch', args.arch]
    if args.output_tar:
        docker_cmd += ['--output-tar', base_output_dir]
    if args.distro:
        docker_cmd += ['--distro', args.distro]
    if args.docker_image:
        docker_cmd += ['--docker-image', args.docker_image]
    docker_cmd += ['--_in-docker']   # prevent recursive re-invocation

    logger.info(f"Running create_data_tar.py inside container '{image_name}' ...")
    res = subprocess.run(docker_cmd, check=False)
    return res.returncode


def check_required_tools() -> None:
    """
    Verify that rpm, rpm2cpio, and cpio are available before doing any work,
    so a missing dependency is reported once upfront instead of surfacing
    later from whichever call happens to need it first.
    """
    missing = [tool for tool in ('rpm', 'rpm2cpio', 'cpio') if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(
            f"Missing required tool(s): {', '.join(missing)}. "
            "Install the rpm and cpio packages to run this script."
        )


def find_srpm_file(path_to_rpms: str) -> str:
    """
    Return the path to the .src.rpm file to use.
    If path_to_rpms is a .src.rpm file path, use it.
    If it is a directory, find the newest *.src.rpm in that directory.
    Defaults to the current working directory (PWD).
    """
    if not path_to_rpms:
        path_to_rpms = '.'

    path_to_rpms = os.path.abspath(path_to_rpms)

    if os.path.isfile(path_to_rpms) and path_to_rpms.endswith('.src.rpm'):
        return path_to_rpms

    if os.path.isdir(path_to_rpms):
        candidates = glob.glob(os.path.join(path_to_rpms, '*.src.rpm'))
        if not candidates:
            raise FileNotFoundError(f"No .src.rpm files found in directory: {path_to_rpms}")
        newest = max(candidates, key=lambda p: os.path.getmtime(p))
        return os.path.abspath(newest)

    raise FileNotFoundError(f"Invalid --path-to-rpms: {path_to_rpms}. Provide a .src.rpm file or a directory containing one.")


def rpm_query(rpm_path: str, queryformat: str) -> str:
    """
    Run `rpm -qp --qf <queryformat>` against an rpm/srpm and return the
    (stripped) first line of stdout.
    """
    try:
        res = subprocess.run(
            ['rpm', '-qp', '--qf', queryformat, rpm_path],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"rpm query failed for {rpm_path}: {e.stderr.strip()}") from e
    return res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""


def collect_binary_rpms_for_srpm(srpm_path: str, work_dir: str):
    """
    Find binary *.rpm files (excluding *.src.rpm) in work_dir whose
    VERSION-RELEASE matches the .src.rpm's, i.e. the subpackages produced by
    the same build.
    Returns a list of rpm paths.
    """
    target_vr = rpm_query(srpm_path, '%{VERSION}-%{RELEASE}')

    matches = []
    for candidate in sorted(glob.glob(os.path.join(work_dir, '*.rpm'))):
        if candidate.endswith('.src.rpm'):
            continue
        try:
            vr = rpm_query(candidate, '%{VERSION}-%{RELEASE}')
        except RuntimeError as e:
            logger.warning(str(e))
            continue
        if vr == target_vr:
            matches.append(candidate)

    if not matches:
        raise RuntimeError(f"No binary .rpm files in {work_dir} match version-release '{target_vr}' from {srpm_path}")
    return matches


def extract_rpms_to_stage(rpm_paths, stage_dir) -> bool:
    """
    For each rpm in rpm_paths, extract its raw payload with
    `rpm2cpio | cpio -idm` directly into stage_dir, merging all rpms'
    contents together (they don't own overlapping paths, since they're
    subpackages of the same build).
    Returns True if at least one rpm was extracted successfully.
    """
    if os.path.isdir(stage_dir):
        shutil.rmtree(stage_dir)
    os.makedirs(stage_dir)

    extracted_any = False
    for rpm_path in rpm_paths:
        if not os.path.exists(rpm_path):
            logger.warning(f"Referenced .rpm not found: {rpm_path} (skipping)")
            continue

        try:
            rpm2cpio = subprocess.Popen(['rpm2cpio', rpm_path], stdout=subprocess.PIPE)
            cpio = subprocess.run(
                ['cpio', '-idm', '--quiet', '--no-absolute-filenames'],
                stdin=rpm2cpio.stdout, cwd=stage_dir, check=True,
            )
            rpm2cpio.stdout.close()
            rpm2cpio.wait()
            if rpm2cpio.returncode != 0:
                raise subprocess.CalledProcessError(rpm2cpio.returncode, 'rpm2cpio')
            extracted_any = True
        except subprocess.CalledProcessError as e:
            logger.error(f"Extraction failed for {rpm_path}: {e}")

    if not extracted_any:
        logger.error("No .rpm files were successfully extracted.")
        return False
    return True


def strip_doc_dirs(stage_dir: str) -> None:
    """
    Remove common documentation directories (usr/share/doc, usr/share/man)
    from the staged content before archiving.
    """
    for doc_rel in DOC_DIR_NAMES:
        doc_path = os.path.join(stage_dir, doc_rel)
        if os.path.isdir(doc_path):
            shutil.rmtree(doc_path)


def gather_notice_and_license(stage_dir: str) -> None:
    """
    NOTICE / LICENSE.qcom-2 ship inside the binary rpms themselves at their
    root, so they land at stage_dir's root during extraction; this just logs
    whether they were found so create_tar_of_stage's normal top-level walk
    picks them up along with everything else.
    """
    for filename in ('NOTICE', 'LICENSE.qcom-2'):
        path = os.path.join(stage_dir, filename)
        if os.path.isfile(path):
            logger.info(f"Including {filename} from the rpm payload in the tarball.")
        else:
            logger.warning(f"No {filename} found in the extracted rpm payload; skipping.")


def create_tar_of_stage(stage_dir: str, tar_path: str) -> str:
    """
    Create tarball at tar_path containing the staged rpm content, with each
    top-level entry of stage_dir added at the tar root (so the archive
    mirrors the rpms' real filesystem layout, e.g. usr/..., etc/...).
    Returns the path to the tarball on success.
    """
    if not os.path.isdir(stage_dir):
        raise RuntimeError(f"Missing staged content to archive: {stage_dir}")

    os.makedirs(os.path.dirname(tar_path) or '.', exist_ok=True)
    with tarfile.open(tar_path, 'w:gz') as tar:
        for entry in sorted(os.listdir(stage_dir)):
            tar.add(os.path.join(stage_dir, entry), arcname=entry)
    return tar_path


def parse_tar_identity(tar_name: str):
    """
    Parse tar name as: <pkg>-<version_release>.<arch>.tar.gz
    Returns dict with pkg/version/arch on success, else None.
    """
    m = re.match(r'^(?P<pkg>.+)-(?P<version>[^-.]+)\.(?P<arch>[^.]+)\.tar\.gz$', tar_name)
    if not m:
        return None
    return m.groupdict()


def fetch_artifactory_uris(query_name: str):
    """
    Query the public Artifactory search API and return list of result URIs.
    """
    query = urllib.parse.urlencode({"name": query_name})
    url = f"{ARTIFACTORY_SEARCH_API}?{query}"
    if not url.startswith("https://"):
        raise RuntimeError(f"Refusing to fetch non-HTTPS URL: {url}")
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Artifactory query failed ({e.code}) for {url}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach Artifactory at {url}: {e}") from e

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Artifactory response was not valid JSON from {url}") from e

    results = data.get("results", [])
    if not isinstance(results, list):
        raise RuntimeError(f"Unexpected Artifactory response shape from {url}: missing list 'results'")
    uris = []
    for row in results:
        if isinstance(row, dict) and isinstance(row.get("uri"), str):
            uris.append(row["uri"])
    return uris


def fail_if_version_exists_in_artifactory(tar_name: str, args) -> None:
    """
    Query Artifactory and fail when the same package version is already present
    for the same architecture.
    """
    identity = parse_tar_identity(tar_name)
    query_name = identity["pkg"] if identity else tar_name

    uris = fetch_artifactory_uris(query_name)

    # Scope duplicate checks to the same distro folder layout used for output:
    # /prebuilt_<distro>/...
    filter_re = re.compile(rf"/prebuilt_{re.escape(args.distro)}/") if args.distro else None
    duplicates = []
    for uri in uris:
        if filter_re and not filter_re.search(uri):
            continue
        base = os.path.basename(uri)
        if not base.endswith(".tar.gz"):
            continue
        if identity:
            other = parse_tar_identity(base)
            if not other:
                continue
            if (other["pkg"] == identity["pkg"]
                    and other["version"] == identity["version"]
                    and other["arch"] == identity["arch"]):
                duplicates.append(uri)
        else:
            if base == tar_name:
                duplicates.append(uri)

    if duplicates:
        examples = "\n".join(f"  - {u}" for u in duplicates[:10])
        extra = "" if len(duplicates) <= 10 else f"\n  ... and {len(duplicates) - 10} more"
        raise RuntimeError(
            "Version already exists in Artifactory; refusing to create tarball.\n"
            f"  tar_name: {tar_name}\n"
            f"  matches: {len(duplicates)}\n"
            f"{examples}{extra}"
        )


def main():
    args = parse_arguments()

    try:
        srpm_path = find_srpm_file(args.path_to_rpms)
    except Exception as e:
        logger.critical(str(e))
        sys.exit(1)

    # Always run inside Docker (as root) to ensure write access to the output
    # directory, which may be owned by root after a container-based rpmbuild.
    if not args.in_docker:
        if shutil.which('docker') is None:
            logger.critical("Missing required tool: docker. Install Docker to run this script.")
            sys.exit(1)
        rc = rerun_in_docker(args, srpm_path)
        sys.exit(rc)

    try:
        check_required_tools()
    except RuntimeError as e:
        logger.critical(str(e))
        sys.exit(1)

    work_dir = os.path.dirname(srpm_path)
    stage_dir = os.path.join(work_dir, '.rpm_stage')

    try:
        name = rpm_query(srpm_path, '%{NAME}')
        version = rpm_query(srpm_path, '%{VERSION}')
        release = rpm_query(srpm_path, '%{RELEASE}')
    except RuntimeError as e:
        logger.critical(str(e))
        sys.exit(1)

    try:
        rpm_paths = collect_binary_rpms_for_srpm(srpm_path, work_dir)
    except Exception as e:
        logger.critical(str(e))
        sys.exit(1)

    arch = args.arch
    if not arch:
        try:
            arch = rpm_query(rpm_paths[0], '%{ARCH}')
        except RuntimeError as e:
            logger.critical(str(e))
            sys.exit(1)

    tar_name = f"{name}-{version}_{release}.{arch}.tar.gz"

    try:
        fail_if_version_exists_in_artifactory(tar_name, args)
    except Exception as e:
        logger.critical(str(e))
        sys.exit(1)

    ok = extract_rpms_to_stage(rpm_paths, stage_dir)
    if not ok:
        shutil.rmtree(stage_dir, ignore_errors=True)
        sys.exit(1)

    gather_notice_and_license(stage_dir)
    strip_doc_dirs(stage_dir)

    try:
        if args.output_tar:
            base_output_dir = os.path.abspath(args.output_tar)
            dest_dir = os.path.join(base_output_dir, f'prebuilt_{args.distro}') if args.distro else base_output_dir
            tar_path = os.path.join(dest_dir, tar_name)
        else:
            dest_dir = os.path.join(work_dir, f'prebuilt_{args.distro}') if args.distro else work_dir
            tar_path = os.path.join(dest_dir, tar_name)
        tar_path = create_tar_of_stage(stage_dir, tar_path)
        logger.info(f"Created tarball: {tar_path}")
    except Exception as e:
        logger.critical(f"Failed to create tarball: {e}")
        sys.exit(1)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"Uncaught exception: {e}")
        traceback.print_exc()
        sys.exit(1)
