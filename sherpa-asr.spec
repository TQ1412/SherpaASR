# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 — 将 sherpa 语音识别系统打包为独立文件夹
运行: pyinstaller sherpa-asr.spec

模型文件、index.html、.env.sherpa 作为外部文件（放在 exe 同级目录），
不打包进 exe，方便用户编辑配置和减小 exe 体积。
"""

import os
import sys
import numpy as _np

# numpy _core 目录（PyInstaller 可能会漏掉）
_NUMPY_DIR = os.path.dirname(_np.__file__)
_NUMPY_CORE_SHIM = os.path.join(_NUMPY_DIR, '_core')

numpy_datas = []
if os.path.isdir(_NUMPY_CORE_SHIM):
    for fname in os.listdir(_NUMPY_CORE_SHIM):
        fpath = os.path.join(_NUMPY_CORE_SHIM, fname)
        if os.path.isfile(fpath):
            numpy_datas.append((fpath, os.path.join('numpy', '_core', fname)))

# Win7 补丁版 api-ms-*.dll 存根目录
_SPEC_DIR = os.path.dirname(os.path.abspath(SPECPATH))
_PATCHED_STUBS_DIR = os.path.join(_SPEC_DIR, 'win7', 'dist', 'SherpaASR')

a = Analysis(
    ['win7/launcher.py'],
    pathex=[],
    binaries=[
        # OpenSSL DLLs (PyInstaller can't find these automatically)
        ('D:/anaconda/envs/sherpa-py37/Library/bin/libssl-1_1-x64.dll', '.'),
        ('D:/anaconda/envs/sherpa-py37/Library/bin/libcrypto-1_1-x64.dll', '.'),
    ],
    datas=numpy_datas,
    hiddenimports=[
        'sherpa_onnx',
        'webrtcvad',
        'noisereduce',
        'numpy',
        'scipy',
        'pydub',
        'soundfile',
        'aiofiles',
        'fastapi',
        'uvicorn',
        'websockets',
        'python_multipart',
        'pywebview',
        'webview',
        'clr_loader',
        'pythonnet',
        'bottle',
        'dotenv',
        'requests',
        'urllib3',
        'asyncio',
        'concurrent.futures',
    ],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 不需要的 GUI / 数据处理库
        'tkinter',
        'matplotlib',
        'pandas',
        'jupyter',
        'IPython',
        'notebook',
        'PIL',
        'PyQt5',
        'PySide2',
        'wx',
        # PyTorch 全家桶（本项目用 sherpa-onnx，不需要 PyTorch）
        'torch',
        'torchvision',
        'torchaudio',
        'functorch',
        # 大型数据处理库
        'pyarrow',
        'Cython',
        # 开发工具
        'test',
        'setuptools',
        'pip',
        'wheel',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# === Win7 兼容：用补丁版 api-ms-* 存根替换 PyInstaller 从 Win10 SDK 收集的 ===
if os.path.isdir(_PATCHED_STUBS_DIR):
    # 收集补丁版存根文件名
    patched_stub_names = {
        fname.lower()
        for fname in os.listdir(_PATCHED_STUBS_DIR)
        if fname.startswith('api-ms-') and fname.endswith('.dll')
    }
    # 过滤掉 Win10 版存根 (TOC格式: name, src_path, typecode)
    filtered_binaries = []
    for b in a.binaries:
        name, src_path, typecode = b
        if name.lower() not in patched_stub_names:
            filtered_binaries.append(b)
    # 加入补丁版
    for fname in os.listdir(_PATCHED_STUBS_DIR):
        if fname.lower() in patched_stub_names:
            fpath = os.path.join(_PATCHED_STUBS_DIR, fname)
            filtered_binaries.append((fname, fpath, 'BINARY'))
    a.binaries = filtered_binaries
    print(f'[Win7 fix] Replaced {len(patched_stub_names)} api-ms-* stubs with patched versions')


pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='sherpa-asr',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SherpaASR',
)
