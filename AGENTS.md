# EasiAuto AI Agent Instructions

## 项目概述

- 项目名为 EasiAuto
- Windows 桌面应用，使用 `PySide6` + `qfluentwidgets` (Fluent Design)
- 为**希沃白板 (EasiNote)**提供自动登录，及通过 ClassIsland 自动触发登录任务

## 项目主要结构速览

```
main.py                  # 入口：调用 Launcher
src/EasiAuto/
  consts.py              # 路径常量、标识符、环境检测
  launcher.py            # 启动、参数解析、单例、命令分发、IPC
  core/
    security.py          # 密码加密
    utils.py             # 工具函数
    automator/           # 四种登录方案 (FIXED/CV/UIA/TOKEN) + AutomationManager
    ipc/                 # IPC client / server
    runtime/             # 异常处理、单例互斥锁
  models/                # 配置、档案的模型及单例
  services/
    binding_service.py   # ClassIsland 绑定同步逻辑
    announcement_service.py  # 公告轮询服务
    update_service.py    # 版本更新检查与安装
  integrations/
    classisland_manager.py   # ClassIsland 配置文件读写接口
  view/
    main_window.py       # 主窗口
    helpers.py           # UI 工具函数
    components/
    pages/
data/                    # 运行时数据
resources/               # 资源
tools/                   # 编译脚本、开发工具、发行中心
vendors/                 # 运行时依赖
```

## 编码规范

- **语言**：注释、日志、UI 文本一律使用**简体中文**
- **代码风格**：使用 Python 3.12 风格
- **Docstring**：简单函数不需要；复杂函数需说明参数、返回值、可能异常
- **标记**：`TODO` 标记待完成，`NOTE` 标记需特别注意

## 注意事项

- 修改工作区时（如新增代码、提交），尽量与已有的内容风格上保持一致
- 需求不明确时，倾向于向用户确认而非直接修改
- 使用 uv 运行环境相关的代码
