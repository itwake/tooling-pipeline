# mvn-rebundle

Re-bundle third-party dependencies from a deployed Spring Boot artifact into one Maven artifact, verified byte for byte.

Extracts selected third-party dependencies from a running Spring Boot artifact (a container image or an executable jar), merges them with maven-shade-plugin into a bundle under new Maven coordinates, verifies the bundle, publishes it to Nexus and proposes the POM-only change for the consuming project. A second pipeline proves that the application rebuilt after the POM change is equivalent to the original one.

Design principles:

- Whole dependency jars are merged. Package names are kept, there is no relocation and no minimization. The consumer's Java code does not change.
- The jars actually running in the artifact are the reference. Bundle verification compares against the bytes taken from the image, so a repository jar that differs from production fails.
- The consumer POM is changed by a person. The pipeline proposes the change; a Maven Enforcer rule is the gate, so any path that still brings an embedded coordinate fails the consumer build.
- Version-aligned families (Spring, Jackson, Netty, ...) and signed jars are refused by default, see `config/policy.json`.

## Layout

| Path | Purpose |
|---|---|
| `Jenkinsfile` | Main pipeline: fetch → inventory → selection and policy → Shade project generation → build → verification → consumer patch proposal → publish |
| `Jenkinsfile.verify` | Verification pipeline: compares the artifacts built before and after the POM change |
| `scripts/*.py` | The stages, standard-library Python only, shared by Jenkins and the local runner |
| `config/policy.json` | Merge policy and the ignore list of the equivalence check |
| `run-local.sh` | Runs the same stages on a workstation |
| `tests/` | Synthetic fixtures and the self-test (no Maven needed) |

## Agent requirements

- Linux agent (`sh` steps).
- JDK 17+, Maven 3.6+ and Python 3.8+ on the PATH.
- For image sources: `docker` (with a reachable daemon) or `crane`. `crane` needs no daemon and suits CI better.
- Jenkins plugins: Pipeline, Git, Credentials Binding (all part of the recommended plugin set).

## Creating the Jenkins jobs

1. Push this repository to your git server.
2. Create a Pipeline job named `mvn-rebundle`, Definition "Pipeline script from SCM", Script Path `Jenkinsfile`.
3. Edit the site settings in the `environment` block at the top of the `Jenkinsfile` once:

   | Variable | Meaning |
   |---|---|
   | `NEXUS_RELEASE_URL` / `NEXUS_SNAPSHOT_URL` | Hosted repositories |
   | `NEXUS_CREDENTIALS_ID` | ID of a username/password credential in Jenkins; empty means anonymous (no publishing) |
   | `MAVEN_MIRROR_URL` | Company Maven group repository; empty resolves from Maven Central |
   | `GIT_CREDENTIALS_ID` | Optional, used to check out the consumer project |

4. The first run only registers the parameters with Jenkins and fails or runs empty; that is expected. Use "Build with Parameters" afterwards.
5. Create a second job named `mvn-rebundle-verify` the same way for the verification pipeline, Script Path `Jenkinsfile.verify`.

## Parameters of the main pipeline

| Parameter | Meaning |
|---|---|
| `SOURCE_TYPE` | `image` or `jar` |
| `SOURCE_REF` | Image reference, or an http(s) URL / agent-local path of the jar |
| `APP_PATH` | Path of the application inside the image. A file for a fat jar; a directory for exploded layouts (`/workspace` for buildpacks images, `/app` for Jib images) |
| `IMAGE_PLATFORM` / `IMAGE_TOOL` | Platform of a multi-arch image; extraction tool auto/docker/crane |
| `SELECT` | Dependencies to merge, one per line: `artifactId`, `groupId:artifactId` or `groupId:artifactId:version` |
| `OUT_GROUP_ID` / `OUT_ARTIFACT_ID` / `OUT_VERSION` | Bundle coordinates. A `-SNAPSHOT` version goes to the snapshot repository |
| `POLICY_MODE` | `strict` fails on policy denials; `warn` downgrades them to warnings. Hard errors fail in both modes |
| `CREATE_SOURCES_JAR` | Also build a `-sources.jar` so IDEs show the sources of the merged libraries |
| `PUBLISH` | `false` builds and verifies only; `true` runs `mvn deploy` after verification |
| `CONSUMER_GIT_URL` / `CONSUMER_GIT_REF` / `CONSUMER_MAVEN_ARGS` | Optional. When given, `dependency:tree -Dverbose` runs on the consumer project and the proposal lists exclusions per module and per path |

Suggested first run: `SOURCE_TYPE=jar`, `SOURCE_REF` pointing at any Spring Boot fat jar, `SELECT=commons-io`, `PUBLISH=false`. Switch to an image source and enable publishing once that works.

## What each stage does

1. **Fetch source**: images are read with `docker create` + `docker cp` (the container is never started) or streamed with `crane export`, extracting only the requested path; jars are downloaded. The image digest and the artifact SHA-256 are recorded.
2. **Inspect application**: detects the layout (fat jar, war, exploded directory, Jib libs directory) and lists every dependency jar with its coordinates (from the jar's own pom.properties), SHA-256, signatures, module-info, Multi-Release, SPI files, Spring metadata, native libraries and nested jars. Jars whose coordinates cannot be identified are listed; select them explicitly as `g:a:v`.
3. **Resolve selection & policy**: resolves the selection to unique jars and applies `policy.json`. Two jars holding the same class path with different bytes cannot be merged; that is a hard error. Counts which SPI / Spring resources occur in several jars to decide which transformers are needed.
4. **Generate bundle project**: writes a standalone Maven project to `work/bundle/`. Dependencies are pinned to the versions found in the artifact; `artifactSet` includes only the selected coordinates; signatures, `INDEX.LIST` and `module-info` are excluded; LICENSE/NOTICE files move to `META-INF/third-party/<artifact>-<version>/`; SPI files use ServicesResourceTransformer, AutoConfiguration.imports and friends use AppendingTransformer, and `spring.factories` present in several jars uses Spring Boot's PropertiesMergingResourceTransformer (version taken from the artifact's Boot). The manifest carries `X-Dependency-Bundle-Contents`, which the verification pipeline reads; `project.build.outputTimestamp` is fixed so the build is reproducible.
5. **Build bundle**: `mvn clean verify`.
6. **Verify bundle**: compares the bundle entry by entry with the original jars from the artifact. Classes and resources must be byte-identical, merged resources must contain every original entry, there must be no extra entries, signatures or module-info, and the dependency-reduced POM must no longer declare the embedded coordinates.
7. **Consumer patch proposal**: writes `out/consumer-patch.md` with the bundle dependency snippet, the direct dependencies to delete, the exclusions to add on each direct dependency, the Enforcer rule and the transitive dependencies the bundle POM still declares.
8. **Publish**: `mvn deploy` when `PUBLISH=true`.
9. Everything is archived under `out/`; `out/SUMMARY.md` is the overview.

## What the consumer has to do

Apply `out/consumer-patch.md` to the POM: delete the replaced direct dependencies, add the bundle dependency, add the proposed exclusions and put the Enforcer rule into the parent POM. Then run `mvn clean verify`; a passing Enforcer check means no path brings the original coordinates back.

Then run the verification pipeline on the artifacts built before and after the change (each may be an image or a jar). It checks that:

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
./run-local.sh verify --old /path/old-app.jar --new /path/new-app.jar
```

Image input: `--type image --ref registry.example.com/team/app:1.2.3 --path /app/app.jar`. On a machine without Maven add `--skip-maven` to stop after project generation. Publishing needs `export NEXUS_USERNAME NEXUS_PASSWORD` plus `--release-url`, `--snapshot-url` and `--publish`.

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
