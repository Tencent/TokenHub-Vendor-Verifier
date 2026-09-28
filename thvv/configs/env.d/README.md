# 命名 env 配置（多模型测试）

为每个待测模型/供应商建一份命名 env，运行时 `--env <name>` 选择，无需反复修改 `configs/.env`：

```bash
cp configs/env.example configs/env.d/glm53.env    # 每个模型一份（如 kimi.env / hy4.env ...）
vim configs/env.d/glm53.env                       # 填写该模型的 API_URL / API_KEY / MODEL_NAME ...

# 切换测试（交替跑）
bash quickstart.sh perf bench-all --env glm53
bash quickstart.sh eval bench aime26 --env kimi

# 并行跑（多个终端同时压不同模型；perf 结果目录含模型名，互不干扰）
bash quickstart.sh perf bench-all --env glm53 &
bash quickstart.sh perf bench-all --env kimi &
```

## 规则

- 查找顺序：`configs/env.d/<name>.env` → `configs/.env.<name>`（散放兼容形式）
- 不带 `--env` 时使用默认 `configs/.env`；等价环境变量 `THVV_ENV=<name>`
- 配置优先级：CLI 参数（`--url` 等）> 外部环境变量 > `--env` 选中的文件 > `configs/.env`
- 本目录（除本 README）已被 `.gitignore` 排除，密钥不入库
- `--env` 名不存在时报错并列出全部可用命名 env
