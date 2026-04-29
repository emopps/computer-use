import subprocess
import os

def get_system_state():
    """获取系统基础状态（非视觉），减少视觉模型冗余调用"""
    state = "【系统状态报告】\n"
    
    # 1. 获取当前窗口列表
    try:
        proc = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ".*"], capture_output=True, text=True)
        wids = proc.stdout.strip().split("\n")
        windows = []
        for wid in wids:
            if not wid: continue
            name_proc = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True, text=True)
            windows.append(name_proc.stdout.strip())
        state += f"- 当前可见窗口: {', '.join(windows) if windows else '无'}\n"
    except:
        state += "- 窗口探测失败\n"

    # 2. 检查特定应用是否运行
    for app in ["et", "wps", "qaxbrowser", "google-chrome", "firefox"]:
        if os.system(f"pgrep {app} > /dev/null") == 0:
            state += f"- 应用进程已启动: {app}\n"

    # 3. 检查特定文件是否存在
    important_files = ["~/Desktop/作文列表.xlsx", "~/Desktop/2026合同台账.xlsx"]
    for f in important_files:
        path = os.path.expanduser(f)
        if os.path.exists(path):
            state += f"- 文件已存在: {f}\n"
            
    return state
