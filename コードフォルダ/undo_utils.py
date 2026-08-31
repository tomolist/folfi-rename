"""ファイル整理ノートブック共通の Undo（復元）基盤。

01〜06 のすべてのノートブックはこのモジュールを通してファイルを移動・削除する。
破壊的な操作はすべて `_undo/undo_log.json` に記録され、`undo_last()` で巻き戻せる。

ログの置き場所
--------------
    メインフォルダ/
        _undo/undo_log.json   ← 全工程の操作履歴（スタック構造）
        _trash/               ← 削除の代わりの退避先
        ... 実データ ...

`_undo` と `_trash` は予約名で、`iter_files()` / `walk()` の走査対象から必ず除外される。
これにより、01 が書いたログを 02 の仕分け処理が巻き込んでしまう問題が起きない。

使い方
------
    import undo_utils as uu

    root = uu.select_folder("対象フォルダを選択してください")
    with uu.UndoJournal(root, "05_フォルダ名の付加") as j:
        for f in uu.iter_files(root):
            j.move(f, f.with_name("new_name.csv"))

    # 巻き戻し
    uu.undo_last(root)
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

# ============================================================
# 予約名 — 走査から必ず除外するディレクトリ
# ============================================================
UNDO_DIR_NAME = "_undo"
TRASH_DIR_NAME = "_trash"
RESERVED_DIRS = {UNDO_DIR_NAME, TRASH_DIR_NAME}

LOG_FILE_NAME = "undo_log.json"
LOG_VERSION = 1

# 自動テスト用: この環境変数が設定されていればダイアログを出さずにその値を使う
ENV_TARGET = "FILE_SEIRI_TARGET"


# ============================================================
# フォルダ選択
# ============================================================
def select_folder(title="フォルダを選択してください"):
    """フォルダ選択ダイアログを開いて Path を返す。

    環境変数 FILE_SEIRI_TARGET が設定されている場合はダイアログを出さず、
    その値をそのまま使う（自動テスト用）。キャンセル時は None を返す。
    """
    env_path = os.environ.get(ENV_TARGET)
    if env_path:
        print(f"[{ENV_TARGET}] {env_path}")
        return Path(env_path)

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # VS Code の裏に隠れるのを防ぐ
    path_str = filedialog.askdirectory(title=title)
    root.destroy()  # Jupyter でフリーズするのを防ぐ

    if not path_str:
        print("フォルダ選択がキャンセルされました。")
        return None

    print(f"選択されたフォルダ: {path_str}")
    return Path(path_str)


# ============================================================
# 走査ヘルパ（予約ディレクトリを除外する）
# ============================================================
def is_reserved(path, root):
    """path が _undo / _trash の中（またはそれ自身）なら True。"""
    path = Path(path)
    root = Path(root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part in RESERVED_DIRS for part in rel.parts)


def walk(root, topdown=True):
    """os.walk のラッパ。_undo / _trash 以下には降りない。

    yield: (Path(dirpath), dirnames, filenames)
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # topdown=True の間に dirnames を書き換えて降下を止める
        dirnames[:] = [d for d in dirnames if d not in RESERVED_DIRS]
        if is_reserved(dirpath, root):
            continue
        yield Path(dirpath), dirnames, filenames


def iter_files(root, pattern="*", skip_hidden=True):
    """root 以下のファイルを再帰的に返す。_undo / _trash は除外。

    pattern は rglob と同じ書式（例: "*.csv"）。
    skip_hidden=True なら "." で始まるファイルを飛ばす。
    """
    root = Path(root)
    for path in sorted(root.rglob(pattern)):
        if not path.is_file():
            continue
        if is_reserved(path, root):
            continue
        if skip_hidden and path.name.startswith("."):
            continue
        yield path


def iter_dirs(root, skip_hidden=True):
    """root 以下のディレクトリを再帰的に返す。_undo / _trash は除外。"""
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_dir():
            continue
        if is_reserved(path, root):
            continue
        if skip_hidden and path.name.startswith("."):
            continue
        yield path


# ============================================================
# ログの読み書き
# ============================================================
def _log_path(root):
    return Path(root) / UNDO_DIR_NAME / LOG_FILE_NAME


def _load_log(root):
    path = _log_path(root)
    if not path.exists():
        return {"version": LOG_VERSION, "steps": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != LOG_VERSION:
        raise ValueError(
            f"ログ形式のバージョンが違います（{data.get('version')}）。\n"
            f"旧形式の undo_log.json は読めません: {path}"
        )
    return data


def _save_log(root, data):
    path = _log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# ジャーナル（1工程 = 1ステップ）
# ============================================================
class UndoJournal:
    """1工程分の破壊的操作を記録するコンテキストマネージャ。

    with を抜けるときにログへ1ステップ追記する（操作が0件なら書かない）。
    例外で抜けた場合も、そこまでの操作は記録される（途中で失敗しても戻せる）。
    """

    def __init__(self, root, step_name):
        self.root = Path(root)
        self.step_name = step_name
        self.ops = []

    # -- コンテキストマネージャ --------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.commit()
        return False  # 例外は握りつぶさない

    def commit(self):
        """ここまでの操作をログへ追記する。"""
        if not self.ops:
            print("記録すべき変更はありませんでした。")
            return
        data = _load_log(self.root)
        data["steps"].append(
            {
                "step": self.step_name,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "ops": self.ops,
            }
        )
        _save_log(self.root, data)
        print(f"復元ログを保存しました: {_log_path(self.root)}")
        print(f"  ステップ「{self.step_name}」/ 操作 {len(self.ops)} 件")
        self.ops = []

    # -- 記録つきの操作 ----------------------------------------
    def move(self, src, dst):
        """src を dst へ移動（rename）して記録する。

        移動先の親フォルダを自動生成した場合、それも記録するので
        巻き戻したときに空のフォルダが residue として残らない。
        （_trash 以下は _cleanup_empty_trash が掃除するので記録しない）
        """
        src, dst = Path(src), Path(dst)
        if not is_reserved(dst, self.root):
            self.mkdir(dst.parent)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        self.ops.append({"t": "move", "src": str(src), "dst": str(dst)})
        return dst

    def mkdir(self, path):
        """ディレクトリを作成して記録する。既に存在する場合は記録しない。"""
        path = Path(path)
        if path.exists():
            return path
        # parents=True だと途中の階層も一緒に作られるので、1階層ずつ記録する
        missing = []
        p = path
        while not p.exists():
            missing.append(p)
            p = p.parent
        path.mkdir(parents=True, exist_ok=True)
        for p in reversed(missing):
            self.ops.append({"t": "mkdir", "path": str(p)})
        return path

    def rmdir(self, path):
        """空ディレクトリを削除して記録する。空でなければ False を返す。"""
        path = Path(path)
        try:
            path.rmdir()
        except OSError:
            return False
        self.ops.append({"t": "rmdir", "path": str(path)})
        return True

    def trash(self, path):
        """削除の代わりに _trash へ退避して記録する（os.remove の置き換え）。

        メインフォルダからの相対パスを _trash 以下に保つので、
        巻き戻さない場合でも手作業で元の場所が分かる。
        """
        path = Path(path)
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            rel = Path(path.name)

        dest = self.root / TRASH_DIR_NAME / self.step_name / rel
        # 同名がすでに退避済みなら連番を足して衝突を避ける
        if dest.exists():
            i = 1
            while True:
                cand = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
                if not cand.exists():
                    dest = cand
                    break
                i += 1
        return self.move(path, dest)


# ============================================================
# 巻き戻し
# ============================================================
def list_steps(root):
    """ログ内のステップ一覧を表示し、リストを返す。"""
    root = Path(root)
    steps = _load_log(root)["steps"]
    if not steps:
        print("復元できる履歴はありません。")
        return steps

    print(f"復元ログ: {_log_path(root)}")
    print("-" * 60)
    for i, s in enumerate(steps, start=1):
        mark = " ← 次に復元されるステップ" if i == len(steps) else ""
        print(f"{i:>2}. {s['step']}")
        print(f"    {s['timestamp']} / 操作 {len(s['ops'])} 件{mark}")
    print("-" * 60)
    return steps


def undo_last(root, count=1):
    """最後のステップを巻き戻す。count を増やすと複数工程さかのぼれる。"""
    root = Path(root)
    data = _load_log(root)

    for _ in range(count):
        if not data["steps"]:
            print("これ以上さかのぼれる履歴はありません。")
            break

        step = data["steps"].pop()
        print(f"\n復元中: {step['step']}（{step['timestamp']}）")

        ok = 0
        ng = 0
        # 操作を逆順に適用する
        for op in reversed(step["ops"]):
            try:
                if op["t"] == "move":
                    src, dst = Path(op["src"]), Path(op["dst"])
                    if not dst.exists():
                        print(f"  スキップ: {dst} が見つかりません")
                        ng += 1
                        continue
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(src))
                elif op["t"] == "mkdir":
                    p = Path(op["path"])
                    if p.is_dir():
                        try:
                            p.rmdir()
                        except OSError:
                            # 後から中身が増えている場合は消さずに残す
                            print(f"  保持: {p}（空ではありません）")
                elif op["t"] == "rmdir":
                    Path(op["path"]).mkdir(parents=True, exist_ok=True)
                else:
                    print(f"  不明な操作をスキップ: {op}")
                    ng += 1
                    continue
                ok += 1
            except Exception as e:
                print(f"  復元エラー: {op} -> {e}")
                ng += 1

        print(f"  完了: {ok} 件を復元" + (f" / {ng} 件を復元できませんでした" if ng else ""))
        # 1ステップ戻すごとに保存する（途中で中断しても整合が取れる）
        _save_log(root, data)

    _cleanup_empty_trash(root)
    return data


def _cleanup_empty_trash(root):
    """復元で空になった _trash 以下のディレクトリを掃除する。"""
    trash = Path(root) / TRASH_DIR_NAME
    if not trash.is_dir():
        return
    for path in sorted(trash.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        trash.rmdir()
    except OSError:
        pass


def undo_interactive(title="復元したいメインフォルダを選択してください"):
    """全ノートブック共通の「復元セル」の中身。

    フォルダを選び、履歴を表示してから直前の1工程を巻き戻す。
    """
    root = select_folder(title)
    if root is None:
        return

    if not _log_path(root).exists():
        print(f"復元用のログが見つかりません: {_log_path(root)}")
        print("このフォルダでは、まだ記録つきの処理が実行されていません。")
        return

    steps = list_steps(root)
    if not steps:
        return

    undo_last(root)
    print("\n復元が完了しました。")
