# dep-bundle-pipeline

从一个正在运行的 Spring Boot 制品（Docker 镜像或可执行 jar）里提取指定的第三方依赖，用 maven-shade-plugin 合并成一个新坐标的 bundle，校验后发布到 Nexus，并给出消费方工程只改 POM 的修改建议。另外提供一条独立的校验流水线，证明改 POM 之后重新构建的应用与原来等价。

设计原则：

- 按完整的依赖 jar 合并，保持原包名，不做 relocation，不做 minimize。消费方 Java 代码不动。
- 以制品里实际运行的 jar 为准。bundle 校验直接拿镜像里的字节比对，仓库里同坐标的 jar 若不一致会失败。
- 消费方 POM 的修改由人来做，流水线给建议；Enforcer 规则做门禁，任何路径漏了 exclusion 都会让消费方构建失败。
- 家族依赖（Spring、Jackson、Netty 等）、带签名的 jar 默认拒绝，见 `config/policy.json`。

## 目录

| 路径 | 作用 |
|---|---|
| `Jenkinsfile` | 主流水线：提取 → 清点 → 选择与策略 → 生成 Shade 工程 → 构建 → 校验 → 消费方建议 → 发布 |
| `Jenkinsfile.verify` | 校验流水线：比对改 POM 前后两个制品 |
| `scripts/*.py` | 各阶段的实现，只依赖 Python 标准库，Jenkins 和本地共用 |
| `config/policy.json` | 合并候选策略与等价性比对的忽略清单 |
| `run-local.sh` | 在工作站上跑同样的阶段 |
| `tests/` | 合成夹具与自测（不需要 Maven） |

## Agent 要求

- Linux agent，`sh` 步骤。
- JDK 17+、Maven 3.6+、Python 3.8+ 在 PATH 上。
- 镜像输入：`docker`（能访问 daemon）或 `crane` 二选一。`crane` 不需要 daemon，更适合 CI。
- Jenkins 插件：Pipeline、Git、Credentials Binding（标准推荐插件集都有）。

## 在 Jenkins 里创建 Job

1. 把本目录推到一个 git 仓库。
2. 新建 Pipeline Job，Definition 选 "Pipeline script from SCM"，Script Path 填 `Jenkinsfile`。
3. 编辑 `Jenkinsfile` 顶部 `environment` 块里的站点配置，只需改一次：

   | 变量 | 说明 |
   |---|---|
   | `NEXUS_RELEASE_URL` / `NEXUS_SNAPSHOT_URL` | hosted 仓库地址 |
   | `NEXUS_CREDENTIALS_ID` | Jenkins 里 username/password 类型凭据的 ID；留空则匿名，不能发布 |
   | `MAVEN_MIRROR_URL` | 公司 Maven group 仓库；留空直接访问 Maven Central |
   | `GIT_CREDENTIALS_ID` | 可选，检出消费方工程用 |

4. 第一次运行只是向 Jenkins 注册参数，会立即失败或跑空，这是正常的。之后用 "Build with Parameters"。
5. 校验流水线同样方式再建一个 Job，Script Path 填 `Jenkinsfile.verify`。

## 主流水线参数

| 参数 | 说明 |
|---|---|
| `SOURCE_TYPE` | `image` 或 `jar` |
| `SOURCE_REF` | 镜像引用，或 jar 的 http(s) 地址 / agent 本地路径 |
| `APP_PATH` | 镜像内应用路径。fat jar 填文件路径；解包布局填目录（buildpacks 镜像为 `/workspace`，Jib 镜像为 `/app`） |
| `IMAGE_PLATFORM` / `IMAGE_TOOL` | 多架构镜像选平台；提取工具 auto/docker/crane |
| `SELECT` | 要合并的依赖，一行一个：`artifactId`、`groupId:artifactId` 或 `groupId:artifactId:version` |
| `OUT_GROUP_ID` / `OUT_ARTIFACT_ID` / `OUT_VERSION` | bundle 坐标。`-SNAPSHOT` 发到 snapshot 仓库 |
| `POLICY_MODE` | `strict` 策略拒绝即失败；`warn` 降级为警告继续。硬性错误两种模式都失败 |
| `CREATE_SOURCES_JAR` | 同时生成 `-sources.jar`，IDE 能看到被合并库的源码 |
| `PUBLISH` | `false` 只构建与校验；`true` 校验通过后 `mvn deploy` |
| `CONSUMER_GIT_URL` / `CONSUMER_GIT_REF` / `CONSUMER_MAVEN_ARGS` | 可选。提供后跑 `dependency:tree -Dverbose`，生成逐模块、逐路径的 exclusion 建议 |

建议第一次这样试：`SOURCE_TYPE=jar`，`SOURCE_REF` 指向任意一个 Spring Boot fat jar，`SELECT=commons-io`，`PUBLISH=false`。跑通后再换镜像输入，再打开发布。

## 流水线每一步做什么

1. **Fetch source**：镜像用 `docker create` + `docker cp`（不启动容器），或 `crane export` 流式提取指定路径；jar 直接下载。记录镜像摘要和制品 SHA-256。
2. **Inspect application**：识别布局（fat jar、war、解包目录、Jib 的 libs 目录），列出每个依赖 jar 的坐标（来自 jar 内的 pom.properties）、SHA-256、签名、module-info、Multi-Release、SPI、Spring 元数据、原生库、嵌套 jar。无法识别坐标的 jar 会列出来，需要用 `g:a:v` 显式选择。
3. **Resolve selection & policy**：把选择解析成唯一的 jar，应用 `policy.json`。同路径 class 字节不同的两个 jar 不能合并，这是硬性错误。统计哪些 SPI / Spring 资源出现在多个 jar 里，决定需要哪些 transformer。
4. **Generate bundle project**：生成独立的 Maven 工程 `work/bundle/`。依赖钉在制品里的版本；`artifactSet` 只包含选中的坐标；排除签名、`INDEX.LIST`、`module-info`；LICENSE/NOTICE 搬到 `META-INF/third-party/<artifact>-<version>/`；SPI 用 ServicesResourceTransformer，AutoConfiguration.imports 等用 AppendingTransformer，`spring.factories` 出现在多个 jar 时用 Spring Boot 的 PropertiesMergingResourceTransformer（版本跟随制品里的 Boot）；Manifest 写入 `X-Dependency-Bundle-Contents`，供校验流水线识别；`project.build.outputTimestamp` 固定，构建可重复。
5. **Build bundle**：`mvn clean verify`。
6. **Verify bundle**：拿制品里的原始 jar 逐条比对 bundle，class 与资源字节必须一致，合并类资源必须包含全部原条目，不能有多余条目、签名或 module-info，dependency-reduced POM 不能再声明被内嵌的坐标。
7. **Consumer patch proposal**：生成 `out/consumer-patch.md`：bundle 依赖片段、要删除的直接依赖、每个直接依赖上要加的 exclusion、Enforcer 规则、bundle 发布 POM 仍保留的传递依赖。
8. **Publish**：`PUBLISH=true` 时 `mvn deploy`。
9. 所有输出归档在 `out/`，`out/SUMMARY.md` 是总览。

## 消费方要做的事

按 `out/consumer-patch.md` 改 POM：删除被替换的直接依赖，加入 bundle 依赖，在建议的直接依赖上加 exclusion，把 Enforcer 规则放到父 POM。然后 `mvn clean verify`，Enforcer 通过即说明没有路径再引入原坐标。

接着用校验流水线比对改 POM 前后的制品（两个都可以是镜像或 jar）。它会检查：

- 新制品里恰好有一个 bundle，它声明的内嵌坐标在旧制品里全部存在；
- 这些坐标从依赖集合里消失，其它依赖一个不多一个不少；
- 依赖 classpath 上每个 class 和资源字节一致，合并类资源按条目集合比对；
- 应用自身的 class 不变，变化的资源会列出来（`build-info.properties`、`classpath.idx` 等必然变化的文件已忽略）。

最后跑原有测试、重建镜像、冒烟。

## 本地运行

```bash
./run-local.sh bundle --type jar --ref /path/app.jar --select "commons-io" \
    --gav com.company.platform:thirdparty-bundle:1.0.0-SNAPSHOT
```

```bash
./run-local.sh verify --old /path/old-app.jar --new /path/new-app.jar
```

镜像输入：`--type image --ref registry.example.com/team/app:1.2.3 --path /app/app.jar`。没有 Maven 的机器可以加 `--skip-maven` 只跑到生成工程。发布需要先 `export NEXUS_USERNAME NEXUS_PASSWORD` 并传 `--release-url`、`--snapshot-url`、`--publish`。

自测（不需要 Maven，用合成夹具模拟 Shade 的输出）：

```bash
python tests/run_selftest.py
```

## 策略配置

`config/policy.json`：

- `deny_groups`：按 groupId 前缀拒绝。默认拒绝 Spring、Jackson、Netty、Micrometer、Tomcat、Jetty、Log4j2、Logback、SLF4J、Kotlin、Reactor、Hibernate、BouncyCastle、gRPC、protobuf。这些要么是必须版本一致的家族，要么必须保持签名。`POLICY_MODE=warn` 可以放行，但请先读下面的说明。
- `warn_groups`：只警告。
- `refuse_signed_jars`：签名 jar 默认拒绝。
- `equivalence.ignore_app_entries`：等价性比对时忽略的应用级条目。

为什么默认拒绝 spring-core：Spring Framework 各模块必须版本一致，由 Boot BOM 统一管理。把 spring-core 冻结进 bundle 后，每次升级 Boot（包括安全补丁）都要先重建 bundle 并让所有消费方同步，漏掉一步就是启动时的 NoSuchMethodError。commons-io 这类没有家族、没有 SPI、没有签名的纯工具库才是合适的候选。

## 已知限制

- Shade 构建需要在有 JDK 与 Maven 的 agent 上执行，这套脚本在没有 Maven 的机器上只用模拟的 Shade 输出做过自测；第一次真实运行请看 `Verify bundle` 阶段的结果。
- `createSourcesJar` 需要仓库里有各依赖的 `-sources.jar`，没有的只会告警。
- 同一个 groupId:artifactId 在制品里出现多个版本或 classifier 时必须用 `g:a:v` 或 `g:a:v:classifier` 选择。
- 自省自身 jar 位置的库（通过 `getCodeSource()` 找原生库、按 `Package.getImplementationVersion()` 判断版本）合并后行为可能变化，只能靠消费方的测试发现。
- Nexus 的 release 仓库默认不允许重复发布同一版本，重新生成请换版本号。
- 消费方 POM 不会被自动修改，这是有意的设计。
