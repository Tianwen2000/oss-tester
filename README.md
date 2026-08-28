# OSS 数据面测试框架

本文中的路径、Bucket、Endpoint、Region、对象前缀和凭证配置都是示例，**必须替换为实际值**。不要把真实 AK/SK、会话令牌、生产 Endpoint 或业务 Bucket 写进命令、源码、日志和报告。

这是一个面向云 OSS/S3 兼容服务的数据面验收和回归工具。主入口是 `oss_test.py`，使用 boto3 统一执行 SDK 操作；`sigv4.py` 只保留给 boto3 无法覆盖的兼容扩展和签名单元测试。旧的 `oss_cli.py`、`oss_capabilities.py` 及辅助脚本仍可调用，但现在只是兼容转发，不再维护第二套测试逻辑。

## 项目目录架构

```text
oss-tester/
├── tests/
│   ├── test_oss_runner.py    # 配置、安全边界、报告、清理和 fake S3 离线测试
│   └── test_cdn_fixtures.py  # CDN Fixture 生成、流式上传和 manifest 离线测试
├── oss_test.py               # 主程序：CLI、SDK 客户端、测试套件、清理和 JSON 报告
├── cdn_fixtures.py           # CDN 源站 Fixture 生成、上传和 manifest
├── sigv4.py                  # boto3 无法覆盖时使用的独立 SigV4 HTTP fallback
├── oss_cli.py                # 旧 CLI 的受保护兼容入口
├── oss_capabilities.py       # 旧能力演示脚本的安全转发入口
├── check_bucket_read.py      # 只读/数据面兼容入口
├── delete_one_object.py      # 仅允许本次测试前缀的单对象删除入口
├── list_buckets.py           # 只读桶列表兼容入口
├── oss-test.example.json     # 不含凭证的完整配置示例
├── docs/                     # 计量、资源包和支付测试规划
│   ├── build_xmind.py        # 重新生成 XMind 文件的维护脚本
│   ├── oss_usage_billing_test_plan.md
│   └── oss_usage_billing_test_plan.xmind
├── requirements.txt          # 运行依赖
├── requirements-dev.txt      # 离线测试依赖
├── .env.example              # 不含真实 AK/SK 的环境变量模板
├── .gitignore                # 忽略凭证、缓存、报告和本地工具文件
├── README.md                 # 安装、执行、安全边界和维护说明
├── fixtures/cdn/              # 已生成的 CDN 源站测试对象
│   ├── small.txt, cache.txt   # 小对象和缓存头场景
│   ├── large.bin, range.bin   # 流式大文件和 Range 场景
│   ├── gzip.txt               # gzip Content-Encoding 场景
│   ├── redirect/*.html        # 301/302/307/308 源站页面
│   └── errors/*.html          # 403/404/405/416/500/503 源站页面
├── 终端命令记录.txt          # 已脱敏的中文命令汇总
└── reports/                  # 运行时生成的 JSON 报告，默认不提交 Git
```

## 环境和安装

安装和运行本项目之前，必须先登录云厂商控制台创建一个**专用 OSS 测试桶**。不要选择已有业务桶或生产桶。创建完成后，从控制台记录以下信息，后续填写到 `.env`：

```text
OSS_ENDPOINT    OSS/S3 兼容访问地址
OSS_REGION      测试桶所在地域
OSS_BUCKET      专用测试桶名称
```

同时准备仅授权该测试桶的最小权限 AK/SK，或者为测试服务器绑定等价的实例角色/工作负载身份。默认数据面测试需要桶访问、对象读写删除、列表、标签、ACL、版本查询和 Multipart 权限；控制面专项还需要对应的 Bucket 配置权限。请妥善保存控制台显示的凭证，SK 通常只在创建时展示一次。

Linux 服务器直接使用系统 Python，不要求创建虚拟环境。需要 Python 3.10+、Git 和可访问 OSS Endpoint 的网络。

先检查 Python 版本：

```bash
python3 --version
```

如果版本低于 3.10，可以执行下面这一段命令自动识别常见发行版并安装 Python 3.11：

```bash
set -eu
if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  PYTHON_BIN=python3
else
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3.11 python3.11-pip
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3.11 python3.11-pip
  else
    echo "Unsupported package manager; install Python 3.10+ using the server administrator's standard method." >&2
    exit 2
  fi
  PYTHON_BIN=python3.11
fi
"$PYTHON_BIN" --version
"$PYTHON_BIN" -m pip --version
echo "Use $PYTHON_BIN for the remaining project commands."
```

不要默认删除旧 Python，也不要直接替换系统的 `/usr/bin/python3`。Ubuntu、Debian、云初始化脚本和系统运维工具可能依赖发行版自带的 Python。新版本与旧版本并行安装即可；只有确认旧版本是手工安装、没有系统依赖，并经过管理员审批后，才考虑单独卸载。

```bash
git clone --depth 1 https://github.com/Tianwen2000/oss-tester.git oss-tester
cd oss-tester
python3 -m pip install --user \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  -r requirements.txt
```

如果上面的版本检查选择了 `python3.11`，将安装和运行命令中的 `python3` 替换为 `python3.11`。依赖安装必须在 `git clone` 项目并进入目录后执行。上面的命令使用清华 PyPI 镜像加速；如果该镜像不可用，可删除 `--index-url https://pypi.tuna.tsinghua.edu.cn/simple` 后使用 Python 官方源。开发机离线验证可安装开发依赖：

```bash
python3 -m pip install --user \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  -r requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 -m py_compile oss_test.py oss_cli.py oss_capabilities.py cdn_fixtures.py sigv4.py
```

如系统 Python 启用了 PEP 668，按服务器管理规范使用 `--break-system-packages`，或由管理员安装到系统路径。不要提交 `.venv/`、缓存或报告。

## 更新项目

后续在服务器更新代码时，在项目目录执行：

```bash
cd ~/oss-tester
git pull --ff-only
python3 -m pip install --user \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  -r requirements.txt
```

`git pull --ff-only` 只允许无冲突的快进更新，能够避免覆盖服务器上的本地修改。如果命令因本地修改而停止，请先执行 `git status` 检查现场，再由管理员决定如何处理；不要使用强制重置或强制覆盖命令。只有 `requirements.txt` 发生变化时才必须重新安装依赖。

## 凭证和 TLS

优先使用云主机工作负载身份、云 SDK 凭证配置或 `AWS_PROFILE`。也支持由密钥管理系统注入的 `OSS_ACCESS_KEY_ID` 和 `OSS_SECRET_ACCESS_KEY` 环境变量；程序不会把它们复制到配置、日志或报告。禁止把 AK/SK 放进命令行参数。

最直接的本地配置方式是在 `oss-tester` 项目根目录复制示例文件并编辑 `.env`：

```bash
cd ~/oss-tester
cp .env.example .env
chmod 600 .env
```

`cp` 只负责复制模板，不会从云控制台自动读取 Endpoint、Region、Bucket 或 AK/SK；复制完成后必须编辑 `.env` 并填入实际值。后续运行读取的是 `.env`，不是 `.env.example`。如果 `.env` 已经存在并且填过凭证，不要重复执行 `cp .env.example .env`，因为它会覆盖本地配置；需要保留原文件时可使用 `cp -n .env.example .env`。

在 `.env` 中填写专用测试桶信息和最小权限凭证：

> **控制台参数换算注意：** 桶详情中的三个字段不一定都能原样用于 S3 SDK。
>
> - 存储桶名称 `sy-duxie2-5201844379265` 可直接填写为 `OSS_BUCKET`。
> - 所属地域“日本-东京”只是控制台展示名，`OSS_REGION` 应填写 API 代码 `ap-tokyo-1`。
> - 默认域名 [http://sy-duxie2-5201844379265.cos.ap-tokyo-1.suzakucos.com](http://sy-duxie2-5201844379265.cos.ap-tokyo-1.suzakucos.com) 可能只是对象访问域名，不一定支持 boto3 的完整 S3 API。本环境实际使用的 S3 API Endpoint 是 [http://151.243.153.26:31027](http://151.243.153.26:31027)。Endpoint 应以平台文档或管理员提供的 API 地址为准，否则可能出现 `403`、`InvalidURI`、`NoSuchBucket` 或签名错误。
>
> 上述 HTTP 地址仅适合当前隔离测试环境。正式环境应使用平台提供的 HTTPS Endpoint，避免通过明文连接发送签名请求。

> **AK/SK 使用规范：** AK/SK 应由 IAM/API 密钥管理正式创建，使用专用测试账号和最小桶权限；不要通过 F12 抓包、日志或后端导出的 JSON 获取或长期复用。若 JSON、日志或聊天记录中出现 `accessKey`、`secretKey`、Token 等敏感字段，应立即停止传播并让管理员轮换凭证。AK/SK 只填写到本机 `.env`，不要提交 Git、写入命令行、报告或截图。

```dotenv
OSS_ENDPOINT=https://<oss-endpoint>
OSS_REGION=<region>
OSS_BUCKET=<dedicated-test-bucket>
OSS_ACCESS_KEY_ID=<dedicated-test-access-key>
OSS_SECRET_ACCESS_KEY=<dedicated-test-secret-key>
```

如果使用 `AWS_PROFILE`、云主机实例角色或工作负载身份，则保持 `OSS_ACCESS_KEY_ID` 和 `OSS_SECRET_ACCESS_KEY` 为空。不要同时配置多套凭证，以免误用权限更高的身份。真实 `.env` 已被 `.gitignore` 排除，仓库中只能保留没有真实密钥的 `.env.example`。

`.env` 配置完成后，必须在项目根目录执行命令；Endpoint、Region 和 Bucket 不需要再次写在命令行中。只需通过布尔值检查配置是否被读取，不要打印凭证内容：

```bash
python3 -c 'from dotenv import load_dotenv; import os; load_dotenv(); print({name: bool(os.getenv(name)) for name in ("OSS_ENDPOINT", "OSS_REGION", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_SECRET_ACCESS_KEY")})'
```

如果提示缺少 Endpoint 或 Bucket，请确认当前目录有名为 `.env` 的文件（不是 `.env.txt`），并确认变量名完全是 `OSS_ENDPOINT`、`OSS_REGION`、`OSS_BUCKET`。不要在 Bash 中直接输入 `<endpoint>`、`<region>` 这类占位符；尖括号会被 Shell 当成重定向符号。配置正确后，标准命令可以直接照抄：

```bash
python3 oss_test.py --profile standard --cleanup always \
  --report reports/oss-standard.json --confirm-bucket
```

生产凭证必须使用 HTTPS、证书校验和最小权限。示例配置见 `.env.example`，其中所有值都需要替换。HTTP Endpoint 只适合隔离的本地 fake 服务或明确的兼容性验证，`security` 套件会给出 WARN。

## 数据面测试

数据面是默认测试范围，覆盖 Endpoint 连通性、鉴权、对象上传/下载与摘要校验、覆盖写、Range、列表与分页、复制、删除、对象标签/ACL、版本观察以及 Multipart 全流程。所有对象只写入本次运行的唯一前缀。

- 核心对象：HeadBucket、Put/Head/Get Object、字节数和 SHA-256、Content-Type、Metadata、ETag、覆盖写、Range、ListObjects v1/v2、Prefix/Delimiter、分页、CopyObject、DeleteObject/DeleteObjects、对象标签、对象 ACL 读取、版本和删除标记观察。
- Multipart：CreateMultipartUpload、UploadPart、ListParts、UploadPartCopy、Complete、Abort、未完成上传清理、暂停后继续和完整下载校验。除最后一片外，分片大小强制至少 5 MiB。

请先在云控制台创建一个专用、空闲、权限最小化的测试桶，再从测试服务器执行。测试桶应与生产桶、业务桶完全隔离。

### 最常用的全量数据面命令

完成上述 `.env` 配置后，在项目根目录直接执行：

```bash
python3 oss_test.py --profile standard --cleanup always \
  --report reports/oss-standard.json --confirm-bucket
```

参数说明：

- `--endpoint`、`--region`、`--bucket`：实际 S3 兼容服务和专用测试桶；也可使用 `OSS_ENDPOINT`、`OSS_REGION`、`OSS_BUCKET`。
- `--config oss-test.example.json`：读取不含凭证的 JSON 配置；环境变量和显式 CLI 参数可覆盖其中的值。
- `--profile`：`smoke`、`standard`、`performance`、`multipart`、`security` 或 `control-plane`。`standard` 是 `network,authentication,data,multipart` 全量数据面；`--suites a,b` 可精确覆盖 profile。
- `--prefix`/`--namespace`：每次运行会自动生成 `oss-test/{timestamp-random-id}/` 形式的唯一前缀（对象存储控制台通常显示为测试文件夹），所有测试对象只写入该前缀。
- `--cleanup`：`always`（默认）、`on-success` 或 `never`。无论策略如何，本次前缀下的未完成 Multipart Upload 都会尝试 Abort；对象、版本和删除标记只按本次前缀清理。
- `--timeout`、`--retry-attempts`、`--retry-backoff`：boto3 连接/读取超时、应用层重试次数和指数退避。
- `--report`：JSON 报告路径；默认写入 `reports/oss-test-<run_id>.json`。报告不包含 AK/SK。
- `--set section.option=value`：覆盖非敏感配置，例如 `--set execution.multipart_part_size_mb=8`。
- `--credential-profile`：选择 boto3 shared credentials profile；它与选择测试 profile 的 `--profile` 分开。
- `--confirm-bucket`：明确确认 `.env` 中的 Bucket 是专用测试桶，而不是业务桶或生产桶。

任何 FAIL 都返回非零退出码；中断返回 130 或 `128+signal`。PASS/FAIL/WARN/SKIP、耗时、错误和关键指标都会写入报告。

### 保留对象供控制台查看

如果需要在测试结束后到 OSS 控制台查看真实上传的对象，必须从项目根目录执行，并使用 `--cleanup never`：

```bash
cd ~/oss-tester
python3 oss_test.py --profile standard --cleanup never \
  --report reports/oss-standard-retain.json --confirm-bucket
```

该策略会保留本次运行前缀下的已完成对象、版本和删除标记，但仍会 Abort 本次运行产生的未完成 Multipart Upload。查看对象时搜索本次输出中的 `oss-test/<run-id>/` 前缀；确认完成后，只删除该前缀下的测试对象。命令中的 `oss_test.py` 必须从 `~/oss-tester` 执行，不要先进入 `reports/` 目录。

### 数据面专项示例

Smoke（网络、鉴权和最小对象链路）：

```bash
python3 oss_test.py --profile smoke --confirm-bucket
```

核心对象数据面（不含 Multipart）：

```bash
python3 oss_test.py --suites network,authentication,data --confirm-bucket
```

Multipart（可调整为 8 MiB，仍满足 S3 最小分片约束）：

```bash
python3 oss_test.py --profile multipart --set execution.multipart_part_size_mb=8 --confirm-bucket
```

性能（并发上限由配置校验限制为 8）：

```bash
python3 oss_test.py --profile performance --concurrency 4 --confirm-bucket
```

对象 ACL 按需修改。默认只读取 ACL；`public-read` 必须同时显式确认：

```bash
python3 oss_test.py --suites data --object-acl private --confirm-bucket
```

### 为 CDN 测试准备源站 Fixture

OSS 数据面验收通过后，可以用兼容 CLI 一次生成并上传 CDN 所需的源站对象。它是独立的 Fixture 准备命令，不属于 `standard` profile。命令从 `.env` 读取 Endpoint、Region、Bucket 和凭证，不把凭证写入命令行；必须确认目标是专用测试桶：

```bash
python3 oss_cli.py seed-cdn-fixtures \
  --directory fixtures/cdn \
  --prefix cdn-test \
  --manifest reports/cdn-fixtures-{run_id}.json \
  --confirm-bucket
```

该命令会自动生成并上传 15 个对象：`small.txt`、8 MiB 的 `large.bin`、Range 用 `range.bin`、长缓存 `cache.txt`、gzip 编码的 `gzip.txt`、301/302/307/308 重定向页面和 403/404/405/416/500/503 错误页面。对象会放在类似 `cdn-test/<timestamp-random-id>/` 的测试文件夹下；上传使用文件流，不会把大文件一次性读入内存；每次运行都会追加唯一目录，默认保留对象，不会清理桶或覆盖其他运行。

Manifest 会记录实际 Key、字节数、SHA-256、ETag、Content-Type、Cache-Control、Content-Encoding 和 CDN 规则预期状态，供 CDN 测试项目通过 `--fixture-manifest` 直接读取。`redirect/` 和 `errors/` 文件只是源站内容，直接访问通常仍是 200；要得到 3xx/4xx/5xx，需在 CDN 或源站路由中配置对应规则。命令失败会返回非零退出码，并保留已上传的本次测试文件夹供排查。

部分 S3 兼容网关对 `Content-Encoding: gzip` 支持不完整。如果带该头的上传返回明确的 `InternalError`、`NotImplemented` 或类似兼容性错误，程序会自动重试为不带该头的普通对象，并将整体结果标为 `WARN`（命令仍返回 0）；manifest 会记录 `requested_content_encoding=gzip`、实际 `content_encoding=null`。这表示对象已准备完成，但 CDN gzip 响应头需要单独验证，不能把它当作 gzip 能力 PASS。

如需重新生成本地 Fixture 文件，可添加 `--overwrite`；这只改写项目内 `fixtures/cdn/` 文件，不会删除 OSS 对象。后续 CDN 回归应使用 manifest 中的 Key 和 SHA-256，避免依赖固定对象名。

## 控制面测试

控制面是显式专项，覆盖 Bucket ACL、Policy、Versioning、Lifecycle、Encryption、CORS 和 Bucket Tagging。它不属于默认 `standard` profile，必须使用专用测试桶并传入确认参数；程序会先保存原配置，测试结束后尽可能恢复。

OSS 的对象操作与多数 Bucket 配置通常共用 S3 兼容 SDK 和认证体系，而且桶配置可在专用测试桶中先快照、后修改并尽可能恢复，因此适合做受保护的控制面自动化。相比之下，Redis 控制面多依赖云厂商私有 OpenAPI，并涉及异步的实例、网络、规格、计费和故障切换等基础设施变更，恢复成本和风险更高，通常先保留人工验收；这并不表示 Redis 控制面不能自动化。

云厂商不支持或无法安全恢复的控制面能力会标记 WARN/SKIP。控制面专项命令：

```bash
python3 oss_test.py --suites control-plane \
  --confirm-control-plane --confirm-bucket
```

`public-read`、公开 Policy、不可逆的版本控制变化、删除桶和全量删除不属于默认行为。旧 `oss_cli.py bucket-delete` 还需要 `--confirm-risk --danger-confirm`，但验收流程不应使用删桶命令。

## 报告、对象和清理

每次运行的 `run_id` 和 `test_prefix` 都不同，不会使用固定的 `oss-test/hello.txt`。报告包含 endpoint、region、bucket、profile、suites、每个用例的状态/耗时/错误/指标、清理计数、总体状态、退出码和中断信息，但不包含 AK/SK。清理只匹配当前 `test_prefix`，不会扫描或删除其他对象、版本、删除标记或桶。

`always` 适合验收；`on-success` 在出现 FAIL 时保留对象便于排查；`never` 保留已完成对象但仍会 Abort 本次运行产生的未完成 Multipart Upload。执行前后可在云控制台确认专用桶没有业务对象。

使用统计、资源包管理和支付金额专项计划见 [Markdown 测试计划](docs/oss_usage_billing_test_plan.md)；也可直接用 XMind 打开 [思维导图文件](docs/oss_usage_billing_test_plan.xmind)。

## 兼容性说明

不同云厂商对 S3 API 的覆盖不完全一致。ListObjects v1、对象 Tagging/ACL、ListObjectVersions、UploadPartCopy，以及 Bucket Policy/ACL/Versioning/Lifecycle/Encryption/CORS/Tagging 等控制面能力，明确返回不支持时标记 WARN；UploadPartCopy 会用普通 UploadPart 补位后继续完整性验证。版本控制未启用、公开 Policy 未明确授权或架构不适用时标记 SKIP。单段对象 ETag 不是 MD5 时标记 WARN，并继续以 SHA-256 为权威校验。权限不足、鉴权失败、5xx 重试耗尽、Range/分页/复制/删除失败、内容摘要不一致、清理越界和其他核心数据面错误均标记 FAIL 并返回非零。Multipart 非最后分片小于 5 MiB 会在本地配置校验阶段直接拒绝。

不要提交真实 `.env`、AK/SK、临时文件、缓存、报告或生产对象信息。
