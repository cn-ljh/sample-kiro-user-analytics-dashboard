# ⚡ Kiro 用户分析仪表盘

基于 Streamlit 的 Kiro 使用数据可视化仪表盘。通过 AWS Glue + Athena 连接 S3 中的 Kiro 用户报告数据，以交互式图表展示消息量、对话数、Credits 消耗、客户端类型、用户活跃度等指标。

![Dashboard](images/dashboard-1.png)
![Dashboard](images/dashboard-2.png)
![Dashboard](images/dashboard-3.png)
![Dashboard](images/dashboard-4.png)
![Dashboard](images/dashboard-5.png)

## 架构

```
用户 → Route53 → ALB (HTTPS + Cognito 认证) → ECS Fargate (Streamlit) → Athena → Glue → S3
```

**技术栈：**
- **前端**: Streamlit + Plotly 交互式图表
- **计算**: ECS Fargate（Serverless 容器）
- **负载均衡**: ALB（支持 WebSocket，Streamlit 必需）
- **认证**: Cognito User Pool + ALB 集成认证
- **数据查询**: Athena + Glue Catalog
- **数据存储**: S3（Kiro 日志）
- **IaC**: CloudFormation
- **DNS/HTTPS**: Route53 + ACM 证书

> ⚠️ **为什么不用 App Runner？** Streamlit 依赖 WebSocket 通信，而 App Runner [不支持 WebSocket](https://github.com/aws/apprunner-roadmap/issues/13)，会导致白屏。因此采用 ECS Fargate + ALB 方案。

## 前置条件

- 在 AWS 控制台启用 Kiro 用户报告数据导出（见下方说明）
- [AWS CLI](https://aws.amazon.com/cli/) 已配置凭证
- [Docker](https://www.docker.com/) + buildx（支持跨架构构建）
- 一个域名 + ACM 证书（用于 HTTPS + Cognito 认证）

## 启用 Kiro 用户报告

使用本仪表盘前，需要在 AWS 控制台启用用户报告数据导出。在 Kiro 设置页面配置 S3 存储桶：

![Kiro Settings](images/kiro-settings.png)

启用后，Kiro 将每日向指定 S3 存储桶发送用户报告 CSV 文件。

详细说明参见 [Kiro 用户活动文档](https://kiro.dev/docs/enterprise/monitor-and-track/user-activity/)。

## 快速开始

### 方式一：一键部署（推荐）

```bash
git clone https://github.com/cn-ljh/sample-kiro-user-analytics-dashboard.git
cd sample-kiro-user-analytics-dashboard

# 1. 配置参数
cp .env.example .env
# 编辑 .env，填入你的 AWS 账号信息（见下方配置说明）

# 2. 一键部署（构建镜像 + CloudFormation + Glue Crawler + 健康检查）
./deploy.sh all
```

### 方式二：本地开发

```bash
cd app
cp .env.example .env
# 编辑 .env，填入 Athena/Glue 配置

pip install -r requirements.txt
streamlit run app.py
```

本地访问 `http://localhost:8501`。

## 配置

编辑 `.env` 文件：

| 变量 | 必填 | 说明 |
|------|------|------|
| `AWS_REGION` | 否 (默认 `us-east-1`) | AWS 区域 |
| `S3_DATA_PATH` | **是** | Kiro 日志 S3 路径（格式：`bucket-name/prefix`） |
| `VPC_ID` | **是** | VPC ID |
| `SUBNET_IDS` | **是** | 至少 2 个不同 AZ 的公有子网（逗号分隔） |
| `DOMAIN_NAME` | **是** | Dashboard 域名（如 `kiro.example.com`） |
| `CERTIFICATE_ARN` | **是** | ACM 证书 ARN（需覆盖 DOMAIN_NAME） |
| `HOSTED_ZONE_ID` | 否 | Route53 托管区域 ID（不填则需手动配置 DNS） |
| `IDENTITY_STORE_ID` | 否 | IAM Identity Center Identity Store ID（用于解析用户名） |

S3 数据路径自动构建为：
```
s3://{S3_DATA_PATH}/AWSLogs/{AccountId}/KiroLogs/user_report/{Region}/
```

## deploy.sh 命令

```bash
./deploy.sh all          # 完整部署（构建 + CloudFormation + Crawler + 健康检查）
./deploy.sh build        # 仅构建并推送 Docker 镜像到 ECR
./deploy.sh deploy       # 仅部署 CloudFormation + Lake Formation + Crawler
./deploy.sh update       # 代码更新：构建镜像 + ECS 滚动更新
./deploy.sh crawler      # 运行 Glue Crawler
./deploy.sh health       # 健康检查
./deploy.sh lakeformation # 授权 Lake Formation 权限
```

## CloudFormation 创建的资源

| 资源 | 说明 |
|------|------|
| ECS Cluster + Service | Fargate 容器运行 Streamlit |
| ALB + Target Group | 负载均衡，支持 WebSocket + Sticky Session |
| HTTPS Listener | ACM 证书 + Cognito 认证 |
| HTTP Listener | 自动重定向到 HTTPS |
| Cognito User Pool | 用户认证（邮箱注册/登录） |
| Glue Database + Crawler | 数据编目，每日 2:00 UTC 自动运行 |
| Athena Workgroup | 查询执行，结果存储到专用 S3 桶 |
| S3 Bucket | Athena 查询结果（7 天自动清理） |
| Route53 Record | DNS 解析（可选） |
| IAM Roles | ECS Task Role / Execution Role / Glue Crawler Role |
| Security Groups | ALB (80/443) + ECS (8080 仅 ALB 可达) |
| CloudWatch Logs | 容器日志（14 天保留） |

## 数据结构

Kiro 用户报告 CSV 包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `date` | string | 报告日期 (YYYY-MM-DD) |
| `userid` | string | 用户 ID |
| `client_type` | string | `KIRO_IDE`、`KIRO_CLI` 或 `PLUGIN` |
| `chat_conversations` | integer | 当天对话数 |
| `credits_used` | double | 当天消耗的 Credits |
| `total_messages` | integer | 消息总数（提示 + 工具调用 + 响应） |
| `subscription_tier` | string | 订阅计划 (Pro, ProPlus, Power) |
| `overage_enabled` | string | 是否启用超额 |
| `overage_credits_used` | double | 超额 Credits 使用量 |
| `overage_cap` | double | 超额上限 |
| `profileid` | string | 关联的 Profile ID |

## 仪表盘功能

- **总体指标** — 用户数、消息量、对话数、Credits、超额使用
- **客户端类型分布** — KIRO_CLI vs KIRO_IDE 占比
- **Top 10 用户** — 按消息量排行
- **日活趋势** — 消息、对话、Credits、活跃用户随时间变化
- **按客户端的日趋势** — 分客户端每日折线图
- **Credits 分析** — 用户 Credits 消耗排行，基础 vs 超额占比
- **订阅计划分布** — 各计划的用户数和 Credits
- **用户活跃度分析** — 分层（Power / Active / Light / Idle）
- **用户活动时间线** — 最近活跃时间、活跃天数、可筛选明细表
- **参与度漏斗** — 各阶段转化率

## 项目结构

```
.
├── deploy.sh                           # 自动化部署脚本
├── .env.example                        # 部署配置模板
├── .gitignore                          # Git 忽略规则
├── cloudformation/
│   ├── template.yaml                   # CloudFormation 模板（完整基础设施）
│   └── parameters.json.example         # 参数示例
├── app/
│   ├── app.py                          # Streamlit 仪表盘主程序
│   ├── config.py                       # 环境变量加载器
│   ├── requirements.txt                # Python 依赖
│   ├── Dockerfile                      # 容器镜像定义
│   ├── .streamlit/config.toml          # Streamlit 配置
│   └── .env.example                    # 本地开发配置模板
├── terraform/                          # Terraform 配置（仅用于本地开发）
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tfvars.example
└── images/                             # 截图
```

## 常见问题

### 白屏 / WebSocket 错误
App Runner 不支持 WebSocket。请使用 CloudFormation 模板部署到 ECS Fargate + ALB。

### Athena 查询失败 (AccessDeniedException)
确保指定了 `ATHENA_WORKGROUP` 环境变量，且 ECS Task Role 有对应 workgroup 的权限。

### "Unable to verify/create output bucket"
ECS Task Role 需要 Athena 结果桶的 `s3:GetBucketLocation` 权限。CloudFormation 模板已包含。

### 用户名显示为长 ID
Kiro 日志中的 userid 格式为 `{IdentityStoreId}.{UserId}`，代码已自动处理前缀剥离。如仍显示 ID，检查 `IDENTITY_STORE_ID` 是否配置正确。

### Lake Formation 权限
如果 AWS 账户启用了 Lake Formation，需要额外授权：
```bash
./deploy.sh lakeformation
```

## 安全

参见 [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications)。

## 许可证

本项目采用 MIT-0 许可证。参见 [LICENSE](LICENSE) 文件。
