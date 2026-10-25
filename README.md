# mvn-rebundle

Re-bundle third-party dependencies from a deployed Spring Boot artifact into one Maven artifact, verified byte for byte.

Extracts selected third-party dependencies from a running Spring Boot artifact (a container image or an executable jar), merges them with maven-shade-plugin into a bundle under new Maven coordinates, verifies the bundle, publishes it to Nexus and proposes the POM-only change for the consuming project. A separate pipeline proves that the application rebuilt after the POM change is equivalent to the original one.

Design principles:

- Whole dependency jars are merged. Package names are kept, there is no relocation and no minimization. The consumer's Java code does not change.
- The jars actually running in the artifact are the reference. Bundle verification compares against the bytes taken from the image, so a repository jar that differs from production fails. Publishing uploads exactly the verified bytes; nothing is rebuilt.
- The consumer POM is changed by a person. The pipeline proposes the change; a Maven Enforcer rule is the gate, so any path that still brings an embedded coordinate fails the consumer build.
- Version-aligned families (Spring, Jackson, Netty, ...) and signed jars are refused by default, see `config/policy.json`.

## Jobs

| Job | Script Path | Purpose |
|---|---|---|
| `mvn-rebundle` | `Jenkinsfile` | Fetch → inventory → selection and policy → Shade project generation → build → verification → optional publish |
| `mvn-rebundle-consumer` | `jenkins/Jenkinsfile.consumer` | POM change proposal for a consuming project, optionally per module and per path from its dependency tree |
| `mvn-rebundle-verify` | `jenkins/Jenkinsfile.verify` | Equivalence check of the application built before and after the POM change |

The three jobs are independent. The consumer job takes the bundle either from an archived `mvn-rebundle` build (Copy Artifact plugin) or from explicit coordinates, so it also works for a bundle that is already in Nexus.

## Layout

| Path | Purpose |
|---|---|
| `Jenkinsfile`, `jenkins/` | The three jobs |
| `scripts/*.py` | The stages, standard-library Python only, shared by Jenkins and the local runner |
| `config/policy.json` | Merge policy and the ignore list of the equivalence check |
| `run-local.sh` | Runs the same stages on a workstation (`bundle`, `consumer`, `verify` modes) |
| `tests/` | Synthetic fixtures and the self-test (no Maven needed) |

## Agent requirements

- Linux agent (`sh` steps).
- `mvn-rebundle`: JDK 17+, Maven 3.6+, Python 3.8+ on the PATH; for image sources `docker` (with a reachable daemon) or `crane`. `crane` needs no daemon and suits CI better.
- `mvn-rebundle-consumer`: Python 3.8+; JDK and Maven when a consumer project is analysed.
- `mvn-rebundle-verify`: Python 3.8+; `docker` or `crane` for image inputs.
- Jenkins plugins: Pipeline, Git, Credentials Binding, Copy Artifact (consumer job, unless explicit coordinates are used).

## Creating the Jenkins jobs

1. Push this repository to your git server.
2. Create the three Pipeline jobs, Definition "Pipeline script from SCM", with the Script Paths from the table above.
3. Edit the site settings in the `environment` block at the top of `Jenkinsfile` once (the consumer job has its own, smaller block):

   | Variable | Meaning |
   |---|---|
   | `NEXUS_RELEASE_URL` / `NEXUS_SNAPSHOT_URL` | Hosted repositories |
   | `NEXUS_CREDENTIALS_ID` | ID of a username/password credential in Jenkins; empty means anonymous (no publishing) |
   | `MAVEN_MIRROR_URL` | Company Maven group repository; empty resolves from Maven Central |
   | `GIT_CREDENTIALS_ID` | Consumer job only, optional: credential for checking out the consumer project |

4. The first run of each job only registers its parameters with Jenkins and fails or runs empty; that is expected. Use "Build with Parameters" afterwards.
5. If the consumer job cannot copy artifacts, grant it "Permission to Copy Artifact" in the `mvn-rebundle` job configuration.

## `mvn-rebundle` parameters

| Parameter | Meaning |
|---|---|
| `SOURCE_TYPE` | `image` or `jar` |
| `SOURCE_REF` | Image reference, or an http(s) URL / agent-local path of the jar |
| `APP_PATH` | Path of the application inside the image. A file for a fat jar; a directory for exploded layouts (`/workspace` for buildpacks images, `/app` for Jib images) |
| `IMAGE_PLATFORM` / `IMAGE_TOOL` | Platform of a multi-arch image; extraction tool auto/docker/crane |
| `INVENTORY_ONLY` | Stop after the inventory. Use it first to see which jars and coordinates the artifact contains |
| `SELECT` | Dependencies to merge, one per line: `artifactId`, `groupId:artifactId` or `groupId:artifactId:version` |
| `OUT_GROUP_ID` / `OUT_ARTIFACT_ID` / `OUT_VERSION` | Bundle coordinates. A `-SNAPSHOT` version goes to the snapshot repository |
| `POLICY_MODE` | `strict` fails on policy denials; `warn` downgrades them to warnings. Hard errors fail in both modes |
| `CREATE_SOURCES_JAR` | Also build a `-sources.jar` so IDEs show the sources of the merged libraries |
| `PUBLISH` | `false` builds and verifies only; `true` uploads the verified jar, its dependency-reduced POM and the sources jar with `deploy-file` |

Suggested first runs: `INVENTORY_ONLY=true` against any Spring Boot fat jar (`SOURCE_TYPE=jar`), then `SELECT=commons-io` with `PUBLISH=false`. Switch to an image source and enable publishing once that works.

## `mvn-rebundle-consumer` parameters

| Parameter | Meaning |
|---|---|
| `UPSTREAM_JOB` / `UPSTREAM_BUILD` | The `mvn-rebundle` build to take the bundle from; empty build number means the last successful one |
| `BUNDLE_COORDINATES` / `EMBEDDED_COORDINATES` | Alternative to the upstream build: an existing bundle's `g:a:v` and the coordinates it embeds, one `g:a:v` per line |
| `CONSUMER_GIT_URL` / `CONSUMER_GIT_REF` / `CONSUMER_MAVEN_ARGS` | Optional. When given, `dependency:tree -Dverbose` runs on the consumer project and the proposal lists the direct dependencies to delete and the exclusions to add, per module and per path |

Without `CONSUMER_GIT_URL` the proposal still contains the bundle dependency snippet, the Enforcer rule and the commands to produce the tree locally.

## `mvn-rebundle-verify` parameters

`OLD_*` and `NEW_*` describe the artifacts built before and after the change (each `image` or `jar`, with the path inside the image), plus `IMAGE_PLATFORM`, `IMAGE_TOOL` and an optional `BUNDLE_ARTIFACT_ID` to pin which jar is the bundle.

## What the `mvn-rebundle` stages do

1. **Fetch source**: images are read with `docker create` + `docker cp` (the container is never started) or streamed with `crane export`, extracting only the requested path; jars are downloaded. The image digest and the artifact SHA-256 are recorded.
2. **Inspect application**: detects the layout (fat jar, war, exploded directory, Jib libs directory) and lists every dependency jar with its coordinates (from the jar's own pom.properties), SHA-256, signatures, module-info, Multi-Release, SPI files, Spring metadata, native libraries and nested jars. Jars whose coordinates cannot be identified are listed; select them explicitly as `g:a:v`. With `INVENTORY_ONLY` the job stops here.
3. **Resolve selection & policy**: resolves the selection to unique jars and applies `policy.json`. Two jars holding the same class path with different bytes cannot be merged; that is a hard error. Counts which SPI / Spring resources occur in several jars to decide which transformers are needed.
4. **Generate bundle project**: writes a standalone Maven project to `work/bundle/`. Dependencies are pinned to the versions found in the artifact; `artifactSet` includes only the selected coordinates; signatures, `INDEX.LIST` and `module-info` are excluded; LICENSE/NOTICE files move to `META-INF/third-party/<artifact>-<version>/`; SPI files use ServicesResourceTransformer, AutoConfiguration.imports and friends use AppendingTransformer, and `spring.factories` present in several jars uses Spring Boot's PropertiesMergingResourceTransformer (version taken from the artifact's Boot). The manifest carries `X-Dependency-Bundle-Contents`, which the verify job reads; `project.build.outputTimestamp` is fixed so the build is reproducible.
5. **Build bundle**: `mvn clean verify`.
6. **Verify bundle**: compares the bundle entry by entry with the original jars from the artifact. Classes and resources must be byte-identical, merged resources must contain every original entry, there must be no extra entries, signatures or module-info, and the dependency-reduced POM must no longer declare the embedded coordinates.
7. **Publish to Nexus** (`PUBLISH=true`): checks that the verification passed and that the jar still has the verified SHA-256, then uploads the jar, the dependency-reduced POM and the sources jar with `maven-deploy-plugin:deploy-file`. No rebuild happens between verification and upload.
8. Everything is archived under `out/`; `out/SUMMARY.md` is the overview.

## What the consumer has to do

Run `mvn-rebundle-consumer` (ideally with the consumer's git URL) and apply `out/consumer-patch.md` to the POM: delete the replaced direct dependencies, add the bundle dependency, add the proposed exclusions and put the Enforcer rule into the parent POM. Then run `mvn clean verify`; a passing Enforcer check means no path brings the original coordinates back.

Then run `mvn-rebundle-verify` on the artifacts built before and after the change (each may be an image or a jar). It checks that:

- the new artifact holds exactly one bundle and every coordinate the bundle declares existed in the old artifact;
- those coordinates disappeared from the dependency set and nothing else was removed or added;
- every class and resource on the dependency classpath is byte-identical, merged resources are compared as sets of entries;
- the application's own classes are unchanged; changed resources are listed (files that always change on a rebuild, such as `build-info.properties` and `classpath.idx`, are ignored).

Finally run the existing tests, rebuild the image and smoke-test it.

## Running locally

```bash
./run-local.sh bundle --type jar --ref /path/app.jar --select "commons-io" \
    --gav com.company.platform:thirdparty-bundle:1.0.0-SNAPSHOT
```

```bash
./run-local.sh consumer --tree dependency-tree.txt
```

```bash
./run-local.sh verify --old /path/old-app.jar --new /path/new-app.jar
```

Image input: `--type image --ref registry.example.com/team/app:1.2.3 --path /app/app.jar`. `--inventory-only` stops after the inventory. On a machine without Maven add `--skip-maven` to stop after project generation. Publishing needs `export NEXUS_USERNAME NEXUS_PASSWORD` plus `--release-url`, `--snapshot-url` and `--publish`. The consumer mode reads `out/bundle-manifest.json` from a previous bundle run, or takes `--bundle g:a:v --embedded "g:a:v,g:a:v"`.

Self-test (no Maven needed; the Shade output is simulated from synthetic fixtures):

```bash
python tests/run_selftest.py
```

## Policy

`config/policy.json`:

- `deny_groups`: refused by groupId prefix. By default Spring, Jackson, Netty, Micrometer, Tomcat, Jetty, Log4j2, Logback, SLF4J, Kotlin, Reactor, Hibernate, BouncyCastle, gRPC and protobuf. They are either families whose modules must share one version or jars that must stay signed. `POLICY_MODE=warn` lets them through; read the note below first.
- `warn_groups`: warning only.
- `refuse_signed_jars`: signed jars are refused by default.
- `equivalence.ignore_app_entries`: application-level entries ignored by the equivalence check.

Why spring-core is refused by default: all Spring Framework modules must have the same version, which the Spring Boot BOM manages for you. Once spring-core is frozen inside a bundle, every Boot upgrade (security patches included) first needs a rebuilt bundle and every consumer has to move both in lockstep; missing one step means a NoSuchMethodError at startup. Pure utility libraries such as commons-io, with no family, no SPI and no signature, are the right candidates.

## Known limitations

- The Shade build needs an agent with a JDK and Maven. On a machine without Maven the stages were only self-tested against a simulated Shade output; watch the `Verify bundle` stage on the first real run.
- `createSourcesJar` needs the `-sources.jar` of each dependency in the repository; missing ones only produce a warning.
- When the artifact contains the same groupId:artifactId in several versions or classifiers, select it as `g:a:v` or `g:a:v:classifier`.
- Libraries that introspect their own jar (locating native libraries through `getCodeSource()`, deciding behaviour from `Package.getImplementationVersion()`) may behave differently after merging; only the consumer's tests can catch that.
- Nexus release repositories reject redeploying an existing version; regenerate under a new version.
- The consumer POM is never modified automatically. That is intentional.
