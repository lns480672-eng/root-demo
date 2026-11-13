"""Prepare a complete, independent state directory for an Agent upgrade."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess

DATABASE = "workspace.sqlite3"
MARKER = ".root-coding-agent-production"
TRIAL_MARKER = ".root-coding-agent-trial"


def fingerprint(root):
    for suffix in ("-wal", "-shm", "-journal"):
        if (root / (DATABASE + suffix)).exists():
            raise ValueError("发现 SQLite 日志，请停止旧服务并关闭数据库连接。")
    result = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        relative = path.relative_to(root)
        if stat.S_ISDIR(mode):
            result[relative] = None
        elif stat.S_ISREG(mode):
            digest = hashlib.sha256()
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[relative] = digest.digest()
        else:
            raise ValueError("状态目录只能包含普通文件和目录。")
    return result


def database_identity(root):
    info = (root / DATABASE).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("数据库必须是独立的普通文件。")
    return {"path": str(root), "inode": info.st_ino}


def check_git(root, entries):
    git_root = Path("staged-repositories")
    for relative in entries:
        if git_root not in relative.parents:
            continue
        if (relative.name.endswith(".lock") or relative.name in
                {"alternates", "http-alternates", "gitdir", "commondir", "worktrees", "gc.pid", ".git"}):
            raise ValueError("Git 仓库含锁或外部引用，请先恢复仓库。")
    for repo in sorted((root / git_root).glob("*.git")):
        git = ["git", "--git-dir", str(repo)]
        env = {"PATH": os.environ.get("PATH", os.defpath), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull}
        try:
            config = subprocess.check_output(
                [*git, "config", "--local", "--name-only", "--list"], env=env, stderr=subprocess.PIPE,
            ).decode().splitlines()
            if set(config) - {"core.repositoryformatversion", "core.filemode", "core.bare",
                              "core.ignorecase", "core.precomposeunicode"}:
                raise ValueError("Git 仓库包含未支持的配置，请先核对仓库。")
            if subprocess.check_output([*git, "rev-parse", "--is-bare-repository"], env=env).strip() != b"true":
                raise ValueError("Git 状态目录中包含非裸仓库。")
            subprocess.run([*git, "fsck", "--strict", "--no-dangling"], check=True, env=env,
                           capture_output=True)
        except subprocess.CalledProcessError:
            raise ValueError("Git 仓库完整性校验失败。") from None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_copy(target, entries):
    for relative, checksum in entries.items():
        if checksum is not None:
            with (target / relative).open("rb") as file:
                os.fsync(file.fileno())
    directories = (relative for relative, checksum in entries.items() if checksum is None)
    for relative in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        sync_directory(target / relative)
    sync_directory(target)
    sync_directory(target.parent)


def prepare(source, target):
    source, target = source.resolve(), target.resolve()
    if target.exists():
        raise ValueError("目标目录已存在，请指定新的独立目录。")
    if source in target.parents or not source.is_dir():
        raise ValueError("来源必须是完整状态目录，且目标须位于来源之外。")
    if any((source / marker).exists() for marker in (MARKER, TRIAL_MARKER)):
        raise ValueError("来源必须是尚未启用 Agent 的完整状态目录。")
    original = fingerprint(source)
    if original.get(Path(DATABASE)) is None:
        raise ValueError("来源缺少 workspace.sqlite3。")
    target.mkdir(mode=0o700)
    try:
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        target.chmod(0o700)
        if fingerprint(target) != original or fingerprint(source) != original:
            raise ValueError("状态目录在复制期间发生变化，请停止写入后重试。")
        check_git(target, original)
        with closing(sqlite3.connect((target / DATABASE).as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite 完整性校验失败。")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("SQLite 外键校验失败。")
        sync_copy(target, original)
        staged = target / (MARKER + ".tmp")
        with staged.open("x", encoding="utf-8") as file:
            json.dump(database_identity(target), file)
            file.flush()
            os.fsync(file.fileno())
        staged.replace(target / MARKER)
        sync_directory(target)
    except BaseException:
        shutil.rmtree(target)
        raise


def require_prepared(root):
    root = root.resolve()
    marker = root / MARKER
    if not marker.is_file() or (root / TRIAL_MARKER).exists():
        raise ValueError("生产 Agent 状态副本未准备。")
    if marker.is_symlink() or json.loads(marker.read_text(encoding="utf-8")) != database_identity(root):
        raise ValueError("生产 Agent 副本身份与准备记录不一致。")
