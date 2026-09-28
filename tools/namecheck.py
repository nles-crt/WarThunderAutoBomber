# -*- coding: utf-8 -*-
"""离线结构检查：AST 查未定义名字 + 真实 import 查类成员。无游戏、无第三方依赖。

**为什么需要这个**：本仓库没有测试框架，而大部分控制逻辑只在游戏里跑到 ——
`_navigate_to_airfield` 只有投完弹、飞机还在飞、8111 还在应答时才执行。写错了
一个变量名（比如把 `vs_ref` 写成 `v_ref`）`py_compile` 完全看不出来，只有在
那一局里抛 `NameError` 才发现，而那时飞机已经在俯冲了。

更糟的是**被 `except` 吞掉的情况**：`_target_info_loop` 里

    try:
        ... get_health() ...
    except Exception:
        pass

`get_health` 从没被 import 进 app.py，于是低血量自动跳伞这条路**从来没跑过**，
而且一声不响。

## 两种检查，缺一不可

**① 名字检查（总是跑）。** 收集每个函数的绑定名，和模块级名字 + builtins 对比。
抓的是拼错、忘 import、忘定义。

**② 类结构检查（`--import`，会真的 import 模块）。** 这是被一次真实事故逼出来的：
把 `def helper():` 写在**列 0** 却插在类体中间，那一行会**终止整个类体**，后面
所有 `def` 都变成这个函数的嵌套函数，而不再是类的方法 —— 跑起来就是

    AttributeError: 'App' object has no attribute '_navigate_to_airfield'

语法合法，名字也都解析得到，所以 ① 和 `py_compile` 都放它过去。只有真的把模块
import 进来、拿 `dir(cls)` 对一遍 `self.X` 才发现得了。

所以 `--import` 会收集类里每个方法用到的 `self.名字`，和 import 进来的类的属性
对一遍，报出对不上的。**改完 app.py 的结构（加方法、挪函数）请跑一次**：

    python tools/namecheck.py --import app.py

要 import 的模块会连带加载 `cv2` / `torch`，所以慢几秒，且只在这个开关下才做。

## 它证明什么、不证明什么

只证明"名字解析得到、类成员对得上"。不证明这些名字绑的是**对的东西**，也不管运行期
才出现的属性名。它替代不了真的飞一局。
"""

import ast
import builtins
import importlib
import io
import sys


def undefined_names(path):
    """① 返回 (函数名, 行号, [未定义名字]) 列表。"""
    tree = ast.parse(io.open(path, encoding='utf-8').read())

    # ---- 模块级绑定：import、顶层 def/class、模块级赋值、except as ----
    mod_bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                mod_bound.add(a.asname or a.name.split('.')[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            mod_bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        mod_bound.add(sub.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            mod_bound.add(node.name)

    bad = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        bound = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                bound.add(n.id)
            elif isinstance(n, ast.arg):
                bound.add(n.arg)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)          # 嵌套 def 和它自己的名字
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    bound.add(a.asname or a.name.split('.')[0])
            elif isinstance(n, ast.ExceptHandler) and n.name:
                bound.add(n.name)
        used = {n.id for n in ast.walk(fn)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unknown = sorted(u for u in used
                         if u not in bound and u not in mod_bound
                         and not hasattr(builtins, u))
        if unknown:
            bad.append((fn.name, fn.lineno, unknown))
    return bad


def self_attrs(fn):
    """某个方法里用到的 `self.名字`（只算读取，不算赋值 —— 赋值是合法的实例属性）。"""
    out = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                and isinstance(n.value, ast.Name) and n.value.id == 'self'):
            out.add(n.attr)
    return out


def shape_check(path, modname):
    """② import 模块，核对每个方法的 self.X 是否真的存在。

    "存在" = 类的属性（`dir(cls)`）**或**类里任何地方 `self.X = ...` 赋过值。
    后者是必须的：`gpad` / `master_on` 这些都在 `__init__` 里赋的，`dir(cls)` 看不到，
    只按 `dir` 判会报出上百条假警报，把真问题埋掉。

    返回 (方法名, [缺失属性]) 列表。真正会报出来的是**既没赋过值、也不是类属性**的名字 ——
    也就是"这个方法已经从类里掉出去了"（掉出去之后它的 `self.X = ...` 也不再算在这个类头上）。
    """
    tree = ast.parse(io.open(path, encoding='utf-8').read())
    sys.path.insert(0, '.')
    mod = importlib.import_module(modname)

    bad = []
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        obj = getattr(mod, cls.name, None)
        if obj is None:
            bad.append((cls.name, ['<这个类不在 import 进来的模块里>']))
            continue
        have = set(dir(obj))
        for n in ast.walk(cls):                     # 含嵌套，实例属性通常赋在 __init__
            if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                    and isinstance(n.value, ast.Name) and n.value.id == 'self'):
                have.add(n.attr)
        for m in cls.body:
            if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            missing = sorted(a for a in self_attrs(m) if a not in have)
            if missing:
                bad.append(('%s.%s' % (cls.name, m.name), missing))
    return bad


def main(argv):
    args = [a for a in argv[1:] if not a.startswith('--')]
    do_import = '--import' in argv
    paths = args or ['app.py']
    total = 0

    for p in paths:
        try:
            for name, line, unknown in undefined_names(p):
                total += len(unknown)
                print('%-22s %-24s line %-6d %s'
                      % (p, name, line, ', '.join(unknown)))
        except SyntaxError as exc:
            total += 1
            print('%s: 语法错误 %s' % (p, exc))

    if do_import:
        for p in paths:
            modname = p[:-3].replace('/', '.').replace('\\', '.')
            try:
                for name, missing in shape_check(p, modname):
                    total += len(missing)
                    print('%-22s %-24s self. 上不存在: %s'
                          % (p, name, ', '.join(missing)))
            except Exception as exc:
                print('%s: import 失败（这项检查跳过）: %s' % (p, exc))

    print('problems: %d' % total)
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
