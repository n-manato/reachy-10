#!/usr/bin/env python3
"""SSH公開鍵を全ロボットに1回だけ入れる（パスワード認証を使うのはここだけ）.

    python tools/install_ssh_keys.py
    python tools/install_ssh_keys.py --robot r04 --password ****

なぜ専用ツールが要るか:
Windows の OpenSSH には `ssh-copy-id` が無く、`sshpass` も無い。さらに
`orchestra` が使う SSH は `BatchMode=yes` なのでパスワードプロンプトを一切
出さない（自動処理が固まるのを防ぐため意図的にそうしてある）。結果として
「最初の1回、パスワードで入って鍵を置く」だけができない。そこを paramiko で
埋める。

これが通ればエージェント経路が開く:
    実測18ms（アップロード再生） -> 予測1-5ms（オンロボットエージェント）

冪等。既に同じ鍵が入っていれば何もしない。
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestra.config import Context  # noqa: E402

# Reachy Mini の出荷時既定。LEARNINGS.md に記録されているもの。
DEFAULT_USER = "pollen"
DEFAULT_PASSWORD = "root"


def ensure_keypair(path: Path) -> Path:
    """鍵が無ければ作る。パスフレーズ無し（非対話で使うため）。"""
    pub = path.with_suffix(path.suffix + ".pub") if path.suffix else Path(str(path) + ".pub")
    if path.exists() and pub.exists():
        return pub
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"generating a key: {path}")
    r = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-N", "", "-C",
         "reachy-orchestra"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {r.stderr or r.stdout}")
    return pub


def install(host: str, user: str, password: str, pubkey: str, timeout: float = 15.0) -> str:
    """1台に鍵を追加する。既にあれば何もしない。戻り値は結果の説明。"""
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(
            hostname=host, username=user, password=password,
            timeout=timeout, allow_agent=False, look_for_keys=False,
        )
    except paramiko.AuthenticationException:
        raise RuntimeError("password authentication failed (check user and password)")
    except Exception as e:
        raise RuntimeError(f"connection failed: {type(e).__name__}: {e}")

    try:
        # grep -qxF で完全一致検索。既にあれば追記しない（冪等）。
        # 権限も明示的に直す。ゆるいと sshd が鍵を無視する。
        key = pubkey.strip().replace("'", "'\\''")
        cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"(grep -qxF '{key}' ~/.ssh/authorized_keys && echo ALREADY "
            f"|| (echo '{key}' >> ~/.ssh/authorized_keys && echo ADDED))"
        )
        _, out, err = cli.exec_command(cmd, timeout=timeout)
        result = out.read().decode().strip()
        errtxt = err.read().decode().strip()
        if not result:
            raise RuntimeError(f"unexpected reply: {errtxt or '(empty)'}")
        return result
    finally:
        cli.close()


def verify(host: str, user: str, key: Path, timeout: int = 10) -> bool:
    """鍵だけで（パスワード無しで）入れることを確認する。"""
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "IdentitiesOnly=yes",
         "-i", str(key), f"{user}@{host}", "echo OK"],
        capture_output=True, text=True, timeout=timeout + 10,
    )
    return r.returncode == 0 and "OK" in r.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robot", "-r", action="append",
                    help="target robots (omit for all)")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None,
                    help="omit to try the default, then prompt if that fails")
    ap.add_argument("--key", default=None, help="path to the private key")
    args = ap.parse_args()

    ctx = Context.load()
    user = args.user or ctx.fleet.ssh_user or DEFAULT_USER
    key_path = Path(args.key) if args.key else Path(
        str(ctx.fleet.ssh.get("key", "~/.ssh/reachy_orchestra"))
    ).expanduser()

    pub_path = ensure_keypair(key_path)
    pubkey = pub_path.read_text(encoding="utf-8").strip()
    print(f"public key: {pub_path}")
    print(f"  {pubkey[:58]}...\n")

    robots = ctx.robots.select(args.robot)
    password = args.password or DEFAULT_PASSWORD

    added = already = failed = 0
    need_password_prompt = True

    for r in robots:
        try:
            res = install(r.ip, user, password, pubkey)
            need_password_prompt = False
            if res == "ADDED":
                print(f"  added    {r.id} ({r.ip})")
                added += 1
            else:
                print(f"  present  {r.id} ({r.ip})")
                already += 1
        except RuntimeError as e:
            if "password authentication failed" in str(e) and need_password_prompt:
                print(f"  the default password was rejected ({r.id})")
                try:
                    password = getpass.getpass(f"  password for {user}@{r.ip}: ")
                except (EOFError, KeyboardInterrupt):
                    print("\n  cancelled")
                    return 130
                try:
                    res = install(r.ip, user, password, pubkey)
                    need_password_prompt = False
                    print(f"  {'added  ' if res == 'ADDED' else 'present'}  {r.id} ({r.ip})")
                    added += res == "ADDED"
                    already += res == "ALREADY"
                    continue
                except RuntimeError as e2:
                    e = e2
            print(f"  failed   {r.id} ({r.ip}): {e}")
            failed += 1

    print(f"\nadded {added} / already present {already} / failed {failed}")

    print("\nchecking that key authentication works ...")
    ok = bad = 0
    for r in robots:
        if verify(r.ip, user, key_path):
            ok += 1
        else:
            print(f"  verification failed {r.id} ({r.ip})")
            bad += 1
    print(f"  connected with the key: {ok}/{len(robots)}")

    if ok == len(robots):
        print("\nthe agent path is now available:")
        print("  orchestra fleet provision")
        print("  orchestra fleet agent start")
        print("  orchestra show deploy lamban && orchestra show run lamban")
    return 1 if (failed or bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
