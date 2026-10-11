"""
Parser 路径与运行时写入上下文。
用于统一 mail-statement-parser 内部各模块对 SQLite 库、验证报告、规则及下载目录的解析。
严格保证：
1. 默认路径基于仓库安装位置解析，绝不依赖调用进程的当前工作目录 (cwd)；
2. 读写分离：规则目录为只读资源，不要求位于可写根；写目标严格校验在 writable_roots 范围内；
3. 解析函数不触发任何磁盘写入、建库或网络操作。
"""

import os
from dataclasses import dataclass
from typing import Optional, Tuple, Union
from pathlib import Path


@dataclass(frozen=True)
class ParserPaths:
    db_path: str
    rules_dir: str
    report_dir: str
    download_dir: str
    data_root: str
    writable_roots: Tuple[str, ...]

    def __post_init__(self):
        # 冻结后校验所有路径均为绝对路径
        for attr in ('db_path', 'rules_dir', 'report_dir', 'download_dir', 'data_root'):
            val = getattr(self, attr)
            if not os.path.isabs(val):
                raise ValueError(f"{attr} 必须为绝对路径: {val}")
        for r in self.writable_roots:
            if not os.path.isabs(r):
                raise ValueError(f"writable_root 必须为绝对路径: {r}")


def _resolve_real_target(target: str) -> str:
    """递归解析目标路径，处理不存在子路径和符号链接。"""
    abs_target = os.path.abspath(target)
    curr = abs_target
    components = []
    while not os.path.lexists(curr):
        parent, name = os.path.split(curr)
        if not name or parent == curr:
            break
        components.append(name)
        curr = parent

    real_base = os.path.realpath(curr)
    if components:
        components.reverse()
        return os.path.normpath(os.path.join(real_base, *components))
    return real_base


def validate_write_target(paths: ParserPaths, target: Union[str, Path]) -> str:
    """
    检查规范化目标路径是否位于 paths.writable_roots 声明的可写根目录内。
    若越界、尝试逃逸或前缀伪装（如 /allowed-evil），抛出 ValueError。
    返回规范化后的绝对路径。
    """
    if not target:
        raise ValueError("写入目标路径不能为空")
    target_str = str(target)
    real_target = _resolve_real_target(target_str)

    for root in paths.writable_roots:
        real_root = os.path.realpath(root)
        try:
            common = os.path.commonpath([real_root, real_target])
            if common == real_root:
                return real_target
        except ValueError:
            continue

    raise ValueError(
        f"写目标越界拒绝: '{target_str}' (解析为 '{real_target}') 不在声明的可写根目录下: {paths.writable_roots}"
    )


def _resolve_base(path_val: Union[str, Path], base_dir: str) -> str:
    s = os.path.expanduser(str(path_val))
    if os.path.isabs(s):
        return os.path.abspath(s)
    return os.path.abspath(os.path.join(base_dir, s))


def resolve_parser_paths(
    data_root: Optional[Union[str, Path]] = None,
    db_path: Optional[Union[str, Path]] = None,
    rules_dir: Optional[Union[str, Path]] = None,
    report_dir: Optional[Union[str, Path]] = None,
    download_dir: Optional[Union[str, Path]] = None,
    writable_roots: Optional[Union[Tuple[Union[str, Path], ...], list]] = None,
) -> ParserPaths:
    """
    无副作用地解析 Parser 运行时路径上下文。
    优先级：显式参数 > 对应环境变量 > 仓库稳定数据根。
    默认根绝对不从调用进程 cwd 解析，而是从本模块 __file__ 所在目录解析。
    """
    repo_root = os.path.abspath(os.path.dirname(__file__))

    env_data_root = os.environ.get('MAIL_STATEMENT_DATA_ROOT')
    env_db_path = os.environ.get('MAIL_STATEMENT_DB_PATH')
    env_report_dir = os.environ.get('MAIL_STATEMENT_REPORT_DIR')
    env_download_dir = os.environ.get('MAIL_STATEMENT_DOWNLOAD_DIR')
    env_rules_dir = os.environ.get('MAIL_STATEMENT_RULES_DIR')

    has_explicit_data_root = (data_root is not None) or (bool(env_data_root))
    if data_root is not None:
        raw_data_root = data_root
    elif env_data_root:
        raw_data_root = env_data_root
    else:
        raw_data_root = None

    has_explicit_db = (db_path is not None) or (bool(env_db_path))
    if db_path is not None:
        raw_db = db_path
    elif env_db_path:
        raw_db = env_db_path
    else:
        raw_db = None

    # 1 & 2. 稳定解析 data_root 和 db_path
    # 严格优先级：显式参数 > 对应环境变量 > 默认根
    if data_root is not None:
        explicit_data_root = True
        abs_data_root = _resolve_base(data_root, repo_root)
        if db_path is not None:
            abs_db = _resolve_base(db_path, abs_data_root)
        elif env_db_path:
            abs_db = _resolve_base(env_db_path, abs_data_root)
        else:
            abs_db = os.path.abspath(os.path.join(abs_data_root, 'statements.db'))
    elif db_path is not None:
        explicit_data_root = False
        abs_db = _resolve_base(db_path, repo_root)
        abs_data_root = os.path.dirname(abs_db)
    elif env_data_root:
        explicit_data_root = True
        abs_data_root = _resolve_base(env_data_root, repo_root)
        if env_db_path:
            abs_db = _resolve_base(env_db_path, abs_data_root)
        else:
            abs_db = os.path.abspath(os.path.join(abs_data_root, 'statements.db'))
    elif env_db_path:
        explicit_data_root = False
        abs_db = _resolve_base(env_db_path, repo_root)
        abs_data_root = os.path.dirname(abs_db)
    else:
        explicit_data_root = False
        abs_data_root = repo_root
        abs_db = os.path.abspath(os.path.join(abs_data_root, 'statements.db'))

    # 3. 确定 report_dir
    if report_dir is not None:
        abs_rep = _resolve_base(report_dir, abs_data_root)
    elif env_report_dir:
        abs_rep = _resolve_base(env_report_dir, abs_data_root)
    else:
        abs_rep = os.path.abspath(os.path.join(abs_data_root, 'validation-reports'))

    # 4. 确定 download_dir
    if download_dir is not None:
        abs_dl = _resolve_base(download_dir, abs_data_root)
    elif env_download_dir:
        abs_dl = _resolve_base(env_download_dir, abs_data_root)
    else:
        abs_dl = os.path.abspath(os.path.join(abs_data_root, 'email-downloads'))

    # 5. 确定 rules_dir (只读，默认在 repo_root/rules)
    if rules_dir is not None:
        abs_rules = _resolve_base(rules_dir, repo_root)
    elif env_rules_dir:
        abs_rules = _resolve_base(env_rules_dir, repo_root)
    else:
        abs_rules = os.path.abspath(os.path.join(repo_root, 'rules'))

    # 6. 确定 writable_roots
    if writable_roots is not None:
        resolved_roots = tuple(
            sorted(set(_resolve_base(r, repo_root) for r in writable_roots))
        )
    else:
        # 显式 data_root 时严禁自动扩展外部 DB 父目录，仅且仅允许 abs_data_root
        resolved_roots = (abs_data_root,)

    paths = ParserPaths(
        db_path=abs_db,
        rules_dir=abs_rules,
        report_dir=abs_rep,
        download_dir=abs_dl,
        data_root=abs_data_root,
        writable_roots=resolved_roots,
    )

    # 7. 冲突检测：写目标（DB、报告、下载）必须全部位于 writable_roots 范围内
    for write_target in (abs_db, abs_rep, abs_dl):
        validate_write_target(paths, write_target)

    return paths
