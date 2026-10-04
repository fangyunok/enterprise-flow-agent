# GitHub 发布与运行

独立仓库：[fangyunok/enterprise-flow-agent](https://github.com/fangyunok/enterprise-flow-agent)。代码推送与远端 CI 状态均在仓库 Actions 页面留档，可逐次核对。

## 可提交内容

提交 `src/`、`tests/`、`data/`、`docs/`、README、MIT LICENSE、`pyproject.toml`、`requirements.lock.txt`、`.env.example` 和 `.github/workflows/ci.yml`。业务及检查点数据库、运行目录、虚拟环境、模型权重、SSH 文件和实际密钥在 `.gitignore` 中排除。

`data/demo_seed.json` 和 `src/enterprise_flow/data/demo_seed.json` 应保持逐字节相同，CI 会验证。后一份用于 wheel 安装后运行，无需依赖源码目录存在。

## 发布前检查

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m build --wheel
git diff --check
git status --short
```

CI 四组矩阵检查核心业务、工具、编排、HTTP 及打包；随后建立新的虚拟环境，在源码目录外安装 wheel，实际完成离线流程并启动 HTTP 服务检查页面。

## GitHub 与在线服务

GitHub 仓库用于代码、文档、测试和 CI。GitHub Pages 不能运行 Python 后端；访问网页需要本机或独立服务器运行 `enterprise-flow serve`。默认监听 `127.0.0.1:7861`，登录从内置身份集切换，用于验证权限隔离。面向企业内网部署时，在同一服务边界上替换为统一身份认证，并把业务库与检查点后端迁移到服务型数据库，无需改动工具与事务边界。

没有模型服务时用 `--mode fixture` 即可完整运行；`fixture` 与 `qwen` / `api` 模式的输出都带模式标记，便于区分本次运行使用的决策路径。真实服务使用进程环境变量配置；`.env.example` 本身不被程序加载。
