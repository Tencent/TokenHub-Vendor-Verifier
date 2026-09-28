# 配置目录

本目录存放 THVV 的运行配置。

## 文件

| 文件 | 说明 | 是否提交 git |
|------|------|--------------|
| `env.example` | 配置模板（无敏感信息），复制为 `.env` 后填写真实值 | ✅ 提交 |
| `.env` | 你的实际配置，含 API_KEY 等密钥（隐藏文件） | ❌ 已被 `.gitignore` 忽略 |
| `env.d/<name>.env` | 命名配置（多模型，`--env <name>` 选择），含密钥 | ❌ 已被 `.gitignore` 忽略 |

> `.env` 是 dotenv 约定文件名，shell 用 `source configs/.env` 加载；
> 隐藏文件（以 `.` 开头）默认不在 IDE 文件树显示，但确实存在。
> 模板文件已改名为 `env.example`（非隐藏），方便在文件树直接查看。

## 快速配置

```bash
cp configs/env.example configs/.env
vi configs/.env   # 填写 API_URL / API_KEY / MODEL_NAME / PROTOCOL / TOKENIZER
```

## 多模型命名配置

为每个模型建 `configs/env.d/<name>.env`，运行时 `--env <name>` 选择（详见 `env.d/README.md`）：

```bash
cp configs/env.example configs/env.d/glm53.env
bash quickstart.sh perf bench-all --env glm53
```

优先级：CLI 参数（`--url/--api-key/--model` 等）> 外部环境变量 > `--env` 选中的文件 > `configs/.env`

## 必填项

| 变量 | 说明 |
|------|------|
| `API_URL` | 完整请求路径（含 `/chat/completions` 或 `/v1/messages`）。脚本不做拼接，原样传给下游。 |
| `API_KEY` | API 密钥 |
| `MODEL_NAME` | 模型名（如 `glm-5.2`、`deepseek-v3`） |
| `PROTOCOL` | 协议：`openai`（perf + eval 均支持）或 `anthropic`（perf 支持，Claude 系列） |

## 可选项

详见 `env.example` 文件内注释。
