# mvn-rebundle

Re-bundle third-party dependencies from a deployed Spring Boot artifact into one Maven artifact, verified byte for byte.

Takes a running Spring Boot artifact (a container image, Nexus coordinates, a URL or a jar), merges the dependencies you name together with everything related to them into a bundle under new Maven coordinates, verifies the bundle, publishes it to Nexus and proposes the POM-only change for the consuming project. A separate pipeline proves that the application rebuilt after the POM change is equivalent to the original one.

Design principles:

- Whole dependency jars are merged. Package names are kept, there is no relocation and no minimization. The consumer's Java code does not change.
- Related jars travel together. Selecting one jar pulls in the rest of its family (Spring, Jackson, Tomcat, ...), every jar with the same groupId and version, and the dependencies declared in its own pom.xml, as long as they exist in the application. Nothing is refused for policy reasons; the report explains every addition and warns about the consequences.
- The jars actually running in the artifact are the reference. Bundle verification compares against the bytes taken from the image, so a repository jar that differs from production fails. Publishing uploads exactly the verified bytes; nothing is rebuilt.
- The consumer POM is changed by a person. The pipeline proposes the change; a Maven Enforcer rule is the gate, so any path that still brings an embedded coordinate fails the consumer build, and so does leaving the Spring Boot line the bundle was built from.

## Jobs

| Job | Script Path | Purpose |
|---|---|---|
| `mvn-rebundle` | `Jenkinsfile` | Fetch → inventory → selection and expansion → Shade project generation → build → verification → optional publish |
| `mvn-rebundle-consumer` | `jenkins/Jenkinsfile.consumer` | POM change proposal for a consuming project, per module and per path when its dependency tree is available |
| `mvn-rebundle-verify` | `jenkins/Jenkinsfile.verify` | Equivalence check of the application built before and after the POM change |
| `mvn-rebundle-rework` | `jenkins/Jenkinsfile.rework` | Change a published bundle (merge an uploaded jar or Nexus coordinates into it, replace an embedded component by a newer version) and publish it under a new version |
| `mvn-rebundle-extract` | `jenkins/Jenkinsfile.extract` | Take the application jar out of an image, Nexus coordinates or a URL and keep it as a fingerprinted build artifact |

The five jobs are independent. The consumer job takes the bundle from an archived `mvn-rebundle` build (Copy Artifact plugin) or downloads it from Nexus by coordinates, so it also works for a bundle published long ago; the rework job always starts from Nexus.

## Layout

| Path | Purpose |
|---|---|
| `Jenkinsfile`, `jenkins/` | The five jobs |
| `scripts/*.py` | The stages, standard-library Python only, shared by Jenkins and the local runner |
| `config/policy.json` | Families that must stay together, notes, and the ignore list of the equivalence check |
| `run-local.sh` | Runs the same stages on a workstation (`bundle`, `consumer`, `verify`, `rework`, `extract` modes) |
| `tests/` | Synthetic fixtures, a fake Nexus, and the self-test (no Maven needed) |

## Agent requirements

- Linux agent (`sh` steps).
- `mvn-rebundle`: JDK 17+, Maven 3.6+, Python 3.8+ on the PATH; `docker` (with a reachable daemon) or `crane` for images, plus the `aws` CLI for images in ECR. `crane` needs no daemon and suits CI better.
- `mvn-rebundle-consumer`: Python 3.8+; JDK and Maven when a consumer project is analysed.
- `mvn-rebundle-verify`: Python 3.8+; `docker` or `crane` for image inputs.
- `mvn-rebundle-rework`: JDK 17+, Maven 3.6+, Python 3.8+; no image tools.
- `mvn-rebundle-extract`: Python 3.8+; `docker` or `crane` for image inputs, the `aws` CLI for images in ECR.
- Jenkins plugins: Pipeline, Git, Credentials Binding, Copy Artifact (consumer job, unless coordinates are used), File Parameter (rework job: the uploaded jar is a `stashedFile` parameter, and the Jenkinsfile does not load without the plugin), CloudBees AWS Credentials (only when `AWS_CREDENTIALS_ID` is set).

## Creating the Jenkins jobs

1. Push this repository to your git server.
2. Create the five Pipeline jobs, Definition "Pipeline script from SCM", with the Script Paths from the table above.
3. Edit the site settings in the `environment` block at the top of each Jenkinsfile once. Everything that describes your site lives there, not in the run parameters:

   | Variable | Meaning |
   |---|---|
   | `NEXUS_RELEASE_URL` / `NEXUS_SNAPSHOT_URL` | Hosted repositories the bundle is published to |
   | `NEXUS_MAVEN_REPO_URL` | Group repository: resolves Maven coordinates in `SOURCE_REF` and identifies jars without pom.properties |
   | `NEXUS_DOCKER_REGISTRY` | Host of the Nexus Docker registry; images from it log in with `SOURCE_CREDENTIALS_ID` |
   | `NEXUS_CREDENTIALS_ID` | Username/password credential for publishing; empty means anonymous (no publishing) |
   | `SOURCE_CREDENTIALS_ID` | Username/password credential used to read images, URLs and coordinates hosted on Nexus; may be the same as `NEXUS_CREDENTIALS_ID` |
   | `AWS_CREDENTIALS_ID` | AWS credential for images in ECR (CloudBees AWS Credentials plugin); empty uses the agent's instance role |
   | `MAVEN_MIRROR_URL` | Maven mirror for the bundle build; empty resolves from Maven Central |
   | `BUNDLE_GROUP_ID` | groupId of every bundle this job produces |
   | `DEFAULT_APP_PATH` | Application path inside images: a fat jar file, or `/workspace` for buildpacks images, `/app` for Jib images |
   | `IMAGE_PLATFORM` / `IMAGE_TOOL` | Platform for multi-arch images; docker, crane or auto |
   | `CREATE_SOURCES_JAR` | Also build a `-sources.jar` so IDEs show the merged sources |
   | `GIT_CREDENTIALS_ID` | Consumer job only, optional: credential for checking out the consumer project |

4. The first run of each job only registers its parameters with Jenkins and fails or runs empty; that is expected. Use "Build with Parameters" afterwards.
5. If the consumer job cannot copy artifacts, grant it "Permission to Copy Artifact" in the `mvn-rebundle` job configuration.

## `mvn-rebundle` parameters

| Parameter | Meaning |
|---|---|
| `SOURCE_REF` | Where the application comes from: an image reference, Maven coordinates, an http(s) URL or a path on the agent. The kind of source and the login it needs are detected from the reference, see below |
| `SELECT` | Dependencies to merge, one per line: `artifactId`, `groupId:artifactId`, `groupId:artifactId:version`, or `*` for every jar; `-name` excludes one. Related jars are added automatically. Empty lists the inventory and stops |
| `BUNDLE_ARTIFACT_ID` / `BUNDLE_VERSION` | Coordinates of the bundle under `BUNDLE_GROUP_ID`. A `-SNAPSHOT` version goes to the snapshot repository |
| `PUBLISH` | `false` builds and verifies only; `true` uploads the verified jar, its dependency-reduced POM and the sources jar with `deploy-file` |

### Source detection

| `SOURCE_REF` looks like | Treated as | Login |
|---|---|---|
| `http://...` or `https://...` | Download of the jar/war | Basic auth with `SOURCE_CREDENTIALS_ID` when the host is one of the Nexus hosts (`NEXUS_DOCKER_REGISTRY`, `NEXUS_MAVEN_REPO_URL`, the publish URLs); anonymous otherwise |
| An existing path on the agent | A jar/war file, or an exploded / Jib directory | None |
| `groupId:artifactId:version`, optionally `:packaging[:classifier]` (`com.example:app:1.2.3:war`, `com.example:app:1.2.3:jar:exec`) | Maven coordinates resolved in `NEXUS_MAVEN_REPO_URL`; SNAPSHOT versions through `maven-metadata.xml` | Basic auth with `SOURCE_CREDENTIALS_ID` |
| `<account>.dkr.ecr.<region>.amazonaws.com/...` or `public.ecr.aws/...` | Image in AWS ECR | `aws ecr get-login-password` for the region in the host, then `docker login` / `crane auth login`; AWS credentials from `AWS_CREDENTIALS_ID` or the agent's instance role |
| Image reference on `NEXUS_DOCKER_REGISTRY` | Image in the Nexus Docker registry | `docker login` / `crane auth login` with `SOURCE_CREDENTIALS_ID` |
| Any other image reference | Image in a registry the agent is already logged in to | None |

Append `#/path/inside/the/image` to an image reference when the application is not at `DEFAULT_APP_PATH`. The job runs `extract_source.py --detect` first and binds only the credentials that the detected source needs. The verify job detects its old and new artifacts the same way, so two Nexus coordinates or two ECR tags can be compared directly.

### Selection and expansion

`SELECT` names what you want; the pipeline adds what must come along:

- every jar of the same family present in the application (`families` in `config/policy.json`: Spring, Jackson, Netty, Micrometer, Tomcat, Jetty, Log4j, Logback, SLF4J, Kotlin, Reactor, Hibernate, BouncyCastle, gRPC, protobuf, jjwt);
- every jar with the same groupId and version as a selected jar;
- the compile and runtime dependencies declared in the selected jars' own pom.xml, recursively.

`-name` keeps a jar out even when it is related; the report then warns that the bundle splits something. `*` takes every jar of the application, which is the safest selection of all: nothing can be split. Signed jars are merged with their signatures stripped and a warning; a JCE provider such as BouncyCastle then loads on OpenJDK builds (Temurin, Corretto, Zulu) but is rejected by Oracle JDK.

Two jars holding the same class path with different bytes (commons-logging next to spring-jcl, for example) are kept the way the application resolves them: the copy that comes first in the application's class-loading order (`BOOT-INF/classpath.idx`, or the Jib classpath file) wins. The bundle's dependency order follows that order and Shade keeps the first copy, so the merged jar loads what the application loaded; the report lists what was shadowed. Nothing in the selection stage stops the pipeline any more.

Jars without a Maven descriptor (Tomcat, BouncyCastle, vendor jars) are identified in this order: the dependencies that the other jars of the application declare in their own pom.xml (a Boot starter names the Tomcat jars, for example, which works offline), the Spring Boot BOM of the application's Boot version, the manifest vendor id, and the Nexus search API; every candidate from the repository is confirmed against its `.sha1` before it is accepted. The lookups need `NEXUS_MAVEN_REPO_URL` and `SOURCE_CREDENTIALS_ID`. A jar that still cannot be identified is embedded anyway, under synthetic coordinates `rebundle.unidentified:<artifactId>:<version>-u<sha1>`: the original file is installed into the agent's local Maven repository before the build, the report warns, and the consumer proposal uses the consumer's dependency tree to find the real coordinates for the exclusion. Naming the jar as `groupId:artifactId:version` in `SELECT` replaces the synthetic coordinates with the real ones.

Suggested first runs: `SELECT` empty with `SOURCE_REF` pointing at any Spring Boot fat jar on the agent, to see the inventory; then `SELECT=commons-io` with `PUBLISH=false`; then `*`.

## What the `mvn-rebundle` stages do

1. **Fetch source**: detects the kind of reference and logs in when needed (ECR: `aws ecr get-login-password` for the registry's region; Nexus Docker registry: the Nexus credential), then reads images with `docker create` + `docker cp` (the container is never started) or streams them with `crane export`, extracting only the requested path. Maven coordinates are downloaded from the repository layout with SNAPSHOT versions resolved through `maven-metadata.xml`; URLs are downloaded. The image digest, the Maven coordinates and the artifact SHA-256 are recorded.
2. **Inspect application**: detects the layout (fat jar, war, exploded directory, Jib libs directory) and lists every dependency jar with its coordinates, SHA-1 / SHA-256, declared dependencies, signatures, module-info, Multi-Release, SPI files, Spring metadata, native libraries and nested jars. Jars without pom.properties are identified through the repository as described above. The Spring Boot version is read from the application's own manifest (`Spring-Boot-Version`, written by the Boot Maven and Gradle plugins), else from the name of the `spring-boot` jar, so it never depends on identifying Spring's Gradle-built jars; it selects the Boot BOM that identifies those jars and the properties-merging transformer of the bundle build. `inspect_app.py --spring-boot-version` overrides it. With `SELECT` empty the job stops here.
3. **Resolve selection**: resolves the tokens, adds the related jars, assigns synthetic coordinates to jars that stayed unidentified, and checks that the merge is possible (no conflicting class bytes). Counts which SPI / Spring resources occur in several jars to decide which transformers are needed.
4. **Generate bundle project**: writes a standalone Maven project to `work/bundle/`. Dependencies are pinned to the versions found in the artifact; `artifactSet` includes only the selected coordinates; signatures, `INDEX.LIST` and `module-info` are excluded; LICENSE/NOTICE files move to `META-INF/third-party/<artifact>-<version>/`; SPI files use ServicesResourceTransformer, AutoConfiguration.imports, `spring-autoconfigure-metadata.properties` and friends use AppendingTransformer, properties resources present in several jars (`spring.factories`, `aot.factories`, `spring.components`, `spring-devtools.properties`) use Spring Boot's PropertiesMergingResourceTransformer with the application's Boot version, and a Log4j plugin cache (`Log4j2Plugins.dat`) present in several jars is merged by Log4j's own `Log4j2PluginCacheFileTransformer` (`org.apache.logging.log4j:log4j-transform-maven-shade-plugin-extensions`, version `--log4j-transform-version`, default 0.2.0). The manifest carries the contents and the Boot version; `project.build.outputTimestamp` is fixed so the build is reproducible.
5. **Build bundle**: installs every component into a job-local Maven repository (`work/m2`) from the application's own bytes, with the real POM when the repository has it, then `mvn clean verify` against that repository. The build therefore never depends on the repository holding those versions, and the agent's shared `~/.m2` is left alone. Then `finalize_bundle_pom.py` derives the POM to publish from Shade's dependency-reduced POM. Shade promotes the transitive dependencies of the embedded jars according to their own POMs, not according to the application: a dependency the application had excluded (Spring Boot's starter excludes `tomcat-annotations-api` from `tomcat-embed-core`, because `jakarta.annotation-api` already provides those classes) would otherwise reach every consumer through the bundle, ahead on the classpath. The published POM keeps only the promoted dependencies the application contained, at the application's version; the rest are pruned and listed in `out/bundle-pom.json`.
6. **Verify bundle**: compares the bundle entry by entry with the original jars from the artifact. Classes and resources must be byte-identical, merged resources must contain every original entry, there must be no extra entries, signatures or module-info, and the published POM must neither declare the embedded coordinates nor promote a dependency the application never had.
7. **Publish to Nexus** (`PUBLISH=true`): checks that the verification passed and that the jar still has the verified SHA-256, then uploads the jar, the published POM and the sources jar with `maven-deploy-plugin:deploy-file`. No rebuild happens between verification and upload.
8. Everything is archived under `out/`; `out/SUMMARY.md` is the overview.

## `mvn-rebundle-consumer` parameters

| Parameter | Meaning |
|---|---|
| `UPSTREAM_BUILD` | Build number of the `UPSTREAM_JOB` run to take the bundle from; empty means the last successful one |
| `BUNDLE_COORDINATES` | Alternative to the upstream build: `g:a:v` of a bundle already in Nexus. The jar's manifest lists what it embeds |
| `CONSUMER_GIT_URL` / `CONSUMER_GIT_REF` / `CONSUMER_MAVEN_ARGS` | Optional. When given, `dependency:tree -Dverbose` runs on the consumer project and the proposal lists the direct dependencies to delete and the exclusions to add, per module and per path |
| `APPLY` / `VERIFY_AGAINST` | Apply the proposal on a branch, build with the Enforcer gate, compare the rebuilt archive with the original artifact (or `VERIFY_AGAINST`) and push the branch, see below |

The proposal always contains the bundle dependency snippet and the Enforcer rule. When Spring Boot itself is not inside the bundle, the rule also pins the consumer to the Boot minor line the bundle was built from, so an upgrade of Boot fails the build until the bundle is rebuilt. When Boot is inside the bundle, the `spring-boot-maven-plugin` version has to match the embedded Boot instead: the proposal says so, and the apply step of the consumer job sets it.

**Test jars.** A Maven exclusion and the Enforcer's `groupId:artifactId` pattern see groupId and artifactId only, so excluding an embedded coordinate would also take away its test jars: the `<type>test-jar</type>` of a module, or the `test` classifier jars that a test library such as spring-kafka-test brings for Kafka. The bundle holds only the jars the application ran with, so the proposal treats a test-scope jar under a classifier the bundle does not embed as the separate artifact it is. A direct declaration of it stays; a jar that reached the module only through paths the changes cut is declared directly with `<scope>test</scope>`, next to the dependency that brought it and with exclusions for the embedded coordinates below it; and the Enforcer rule gets one include per jar, `groupId:artifactId:*:*:test:classifier`, which admits it in test scope only (the apply step adds one as well for such a test jar that a profile declares and the tree never showed). A jar under another classifier outside test scope still goes with the embedded coordinate, because such variants usually hold the same classes (`jakarta`, `no_aop`, other platforms' natives); the proposal reports it.

**Version drift.** After the switch the embedded libraries run at the bundle's versions, while everything outside the bundle keeps the versions the consumer manages. With the dependency tree the proposal lists the embedded coordinates the consumer resolves at another version (its Spring Boot parent moved on since the bundle was built, say) and the jars of an embedded release train at another version: `spring-boot-test`, `spring-test`, `spring-kafka-test` or `reactor-test` next to the Spring Boot, Spring Framework, Spring Kafka or Reactor inside the bundle, the usual cause of tests that fail with `NoClassDefFoundError` or `NoSuchMethodError` after the switch. A release train is a groupId whose embedded jars all share one version and that is a policy family or holds at least two of them. Align the consumer with the bundle, or rebuild the bundle. These findings, and the jars under another classifier taken away outside test scope, go to `out/consumer-patch-findings.txt`; they make the build UNSTABLE and are listed with the other findings before the push.

**Build plugins.** With `CONSUMER_GIT_URL` the job also runs `help:effective-pom`, which lists every plugin each module uses, the declared ones and the ones the lifecycle binds by default (compiler, surefire, jar, resources...), with the dependencies the POMs add to them. `plugin_trees.py` turns that list into a throwaway reactor with one module per distinct plugin configuration, and `dependency:tree -Dverbose` on it shows each plugin's class realm the way Maven resolves it: the added dependencies first and dominant, the plugin's provided and test dependencies left out. A plugin whose realm contains an embedded coordinate gets the bundle as its first dependency, in the root POM's `pluginManagement` so every module's use of the plugin inherits it, and the coordinate is taken away as far as Maven allows: a coordinate the POMs add to the plugin is removed, an added dependency that brings one gets an exclusion, a transitive dependency of the plugin itself is cut by re-declaring the plugin's own direct dependency with an exclusion (its version is then pinned in the consumer POM), and a direct dependency of the plugin itself cannot be excluded at all, since `<plugin>` has no exclusions and re-declaring the coordinate still resolves it: the bundle precedes it on the realm classpath, so its classes come from the bundle while the jar stays resolved, and the proposal lists it as shadowed. In `config/policy.json`, `plugins.direct_hits` set to `skip` leaves such plugins untouched instead, and `plugins.skip` names plugins never to touch. Artifacts the Maven core realm exports to every plugin (maven-*, plexus-classworlds, slf4j-api...) never count as hits. No Enforcer rule covers plugin realms, so after the build the job regenerates the plugin trees from the changed POMs and `verify_plugin_trees.py` checks that each patched plugin sees the bundle and no embedded coordinate other than the shadowed ones; its findings make the build UNSTABLE and are listed before the push together with the comparison findings. Locally: run `plugin_trees.py` on an effective POM, `dependency:tree -Dverbose` on its reactor, then `./run-local.sh consumer ... --plugin-tree plugin-trees.txt --plugins consumer-plugins.json`.

### Applying the change automatically (`APPLY=true`)

With `CONSUMER_GIT_URL` and `APPLY=true` the job turns the proposal into a reviewable branch of the consumer project:

1. **Apply patch**: `apply_consumer_patch.py` edits the POMs in place with a position-preserving editor, so comments, element order and formatting stay as they were. It deletes the replaced direct dependencies (a declaration of a test jar under another classifier of the same groupId:artifactId stays), adds the exclusions on the direct dependencies that reintroduce embedded coordinates (merging with existing `<exclusions>`), declares the test jars those exclusions would take away directly with test scope, right after the dependency that brought them, adds the bundle dependency first in every changed module (version managed in the root POM of a multi-module build) with exclusions for the transitive dependencies of the bundle POM that the module must not take from the bundle (it never had them, already reaches them on its own, or only in test scope; what it reaches only through an embedded coordinate stays), and installs or updates the `ban-bundled-coordinates` execution of maven-enforcer-plugin in the root POM, with the includes for those test jars. Dependencies declared in a parent POM inside the checkout are edited there, and so are declarations inside profiles: a replaced coordinate is removed from every profile that declares it, and a direct dependency that needs exclusions gets them on each of its declarations, because an active profile's declaration replaces the main one wholesale. Dependencies that profiles declare but that the dependency tree never showed (the profile was not active when the tree ran) are listed as warnings, since nothing is known about what they bring. When Spring Boot itself is inside the bundle, the version of `spring-boot-maven-plugin` is set to the embedded Boot version wherever the changed modules declare or manage the plugin inside the checkout, so the launcher it writes matches the runtime: a declaration that takes its version from a `spring-boot-starter-parent` of that same version is left alone, one that takes it from another parent outside the checkout gets the version written explicitly, and a checkout that does not declare the plugin at all gets a warning. Build plugins whose dependency tree contains an embedded coordinate are handled as described above: the dependencies the POMs add to them are edited where they are declared, the bundle and the re-declared dependencies go to the root POM's `pluginManagement`. The diff is archived as `out/consumer-apply.diff`; the branch is `rebundle/<bundle artifactId>-<version>`.
2. **Build consumer with the Enforcer gate**: `mvn verify` with `CONSUMER_BUILD_ARGS` (default `-DskipTests`: the tests are compiled, not run; a class the tests only load at run time is not checked here). The Enforcer execution runs at `validate`, so a path that still brings an original coordinate fails here with the offending coordinates in the log. Then the plugin trees are regenerated from the changed POMs and `verify_plugin_trees.py` checks that every patched plugin sees the bundle and no embedded coordinate beyond the shadowed ones; its findings make the build UNSTABLE and are listed with the comparison findings before the push.
3. **Verify rebuilt application**: the Spring Boot archive the build produced is located (the one containing the bundle), the artifact the bundle was built from is fetched again (or `VERIFY_AGAINST`), and `verify_app_equivalence.py` compares the two. The comparison reports, it does not decide: the two builds may come from different commits, so a difference can be the code change as well as the swap. Every difference is listed with where it sits. In the dependencies: a third-party jar removed, added or at another version, an embedded coordinate present at another version in the old build or still on the classpath next to the bundle, a class or resource of the third-party jars missing, new or with different bytes in class-loading order (grouped by the jars that provide them). In the application: the project's own modules rebuilt under another version (the original build was stamped by its CI, say `2.0.9-20260916083542` against the POM's `2.0.9`), their changed entries, and the application's own classes or resources, with entries that embed the build version marked. The source commit of each build is read from `git.properties` (git-commit-id plugin) or `build-info.properties` when present, and the report says whether the builds come from the same commit: if they do, a difference in the dependencies means the swap itself is wrong; if not, judge the list against what changed in the code. The build is marked UNSTABLE when there is anything to review, and `out/SUMMARY.md` is archived at this point so it can be read while the next step waits. When the bundle was taken from Nexus and `VERIFY_AGAINST` is empty, this step is skipped with a message.
4. **Push branch** (`PUSH_BRANCH=true` in the environment block): when there are findings (patch proposal, comparison, plugin dependency check), `ON_DIFFERENCES` decides: `ask` (the default) pauses the job with an input step that lists them and pushes only on approval (no answer within `ON_DIFFERENCES_TIMEOUT` minutes means no push), `push` pushes anyway, `stop` never pushes. The POM changes are committed and pushed with `GIT_CREDENTIALS_ID` (https) or the agent's ssh keys; the commit message records the outcome of the comparison and the build URL. Open the merge request from that branch; nothing is merged automatically.

Only a build error or the Enforcer gate stops the job before the push; what the comparison found is the reviewer's call. Dependencies that the proposal could not locate in the POMs of the checkout (declared in a parent POM outside it) and profile dependencies the tree never showed are listed as warnings in `out/consumer-apply.json` and must be handled by hand.

## `mvn-rebundle-verify` parameters

`OLD_SOURCE_REF` / `NEW_SOURCE_REF` are the artifacts built before and after the change, detected like `SOURCE_REF` above (`#/path` selects the application inside an image), plus an optional `BUNDLE_ARTIFACT_ID` to pin which jar is the bundle. The job lists every difference as the consumer job's step 3 above describes and ends UNSTABLE when there is anything to review; it never fails on a difference. Locally, `./run-local.sh verify ... --fail-on dependencies|any` turns findings of that scope into a non-zero exit for scripts that want a gate.

## `mvn-rebundle-rework` parameters

Changes a bundle that is already in Nexus and publishes the result under a new version, without the application artifact: a jar that was left out, a hot fix for a class inside the bundle, or a newer version of an embedded library.

| Parameter | Meaning |
|---|---|
| `BUNDLE_COORDINATES` | `groupId:artifactId:version` of the published bundle to change |
| `ADD_JAR` | A jar uploaded with the build (File Parameter plugin, `stashedFile`). Its coordinates come from its `pom.properties`, else from the Nexus checksum search confirmed by the `.sha1`, else it is embedded under synthetic coordinates as the main job does |
| `ADD_COORDINATES` | Jars to add from Nexus, one `groupId:artifactId:version[:classifier]` per line; their POMs are fetched too |
| `NEW_VERSION` | Version of the new bundle. Empty derives it: `<version>.1` for a release, counting up when the bundle is itself a rework (`<base>.N`), the same version for a SNAPSHOT. A version Nexus already has is refused before anything is built |
| `PUBLISH` | `false` builds and verifies only; `true` uploads the verified jar, its POM and the sources jar with `deploy-file` |

What happens:

1. **Fetch bundle**: `fetch_bundle.py` downloads the bundle jar, its POM and its sources jar (when published) by coordinates, checks that the jar is an mvn-rebundle bundle, reads its manifest (contents, source, Spring Boot version, origin) and `META-INF/third-party/BUNDLE-CONTENTS.txt` (the SHA-256 of every embedded component), and downloads the `ADD_COORDINATES` jars with their POMs.
2. **Plan rework**: `rework_bundle.py` decides what each added jar does. A coordinate that is not embedded is added. A jar whose `groupId:artifactId` is embedded at another version (or at the same version with other bytes) replaces that component: its old jar is fetched from Nexus and its entries are removed from a copy of the bundle, a class or resource only when the bundle holds the same bytes (a copy that came from another component stays), SPI files, Spring metadata and the Log4j plugin cache lose the lines it contributed, its legal files go. When Nexus does not have the old jar, only the entries the new jar carries too are replaced and the plan warns that entries the new version dropped stay. A jar identical to an embedded component is skipped. Then the Shade project is generated with the added jars first and the bundle copy last, so an entry both have is taken from the added jar (a hot fix wins) and is listed as replaced; merged resources use the transformers of the original build, with the Boot version from the bundle's manifest. The new manifest lists the new contents and records `X-Dependency-Bundle-Reworked-From` (the bundle this one came from) and `X-Dependency-Bundle-Base` (the original build); `BUNDLE-CONTENTS.txt` is rewritten.
3. **Build bundle**: the inputs are installed into the job-local repository (the bundle with the POM it was published with, added coordinates with their POMs, an uploaded jar with the POM Nexus has for its coordinates or a generated one), then `mvn clean verify`. `finalize_bundle_pom.py` derives the POM: the dependencies the previous bundle POM declared stay at their versions, a dependency an added jar's POM brings is kept and reported as new (make sure the consumer has or wants it), and an embedded coordinate an added jar's POM declares is pruned.
4. **Verify bundle**: `verify_bundle.py` compares the result with the inputs the way the main job compares with the application's jars, and also checks that no line of a replaced component survived in a merged resource; a dependency promoted from an added jar is a warning, not a failure.
5. **Publish to Nexus** (`PUBLISH=true`) with `deploy-file`, as in the main job.

Afterwards run `mvn-rebundle-consumer` with the new coordinates: the jar's manifest lists what it embeds now, so the proposal adds the exclusions for the added coordinates and moves the consumer to the new version. Removing a component is not a rework: rebuild the bundle with `mvn-rebundle` and a smaller `SELECT`.

## `mvn-rebundle-extract` parameters

Takes the application archive out of a source and keeps it as a build artifact, for a look at what runs in production or as the input of another job.

| Parameter | Meaning |
|---|---|
| `SOURCE_REF` | Where the application comes from, detected like `SOURCE_REF` of the main job: an image reference (`#/path` when the application is not at `DEFAULT_APP_PATH`), Maven coordinates, an http(s) URL or a path on the agent |
| `ARTIFACT_NAME` | File name of the artifact. Empty derives it: `<repository>-<tag>.jar` for an image (`<repository>-<digest prefix>.jar` without a tag), the artifact's file name for Maven coordinates (SNAPSHOT versions resolved), the file name for a URL or path |

`extract_source.py` fetches the source as in the main job, then `export_app.py` writes `out/artifact/<name>` and `<name>.sha256`: a jar or war byte for byte; an exploded Spring Boot layout (a buildpacks image's `/workspace`) packed back into an executable archive, `META-INF/MANIFEST.MF` first and the nested jars stored without compression as the Boot launcher requires; a Jib layout (`/app` with `libs` and `classes`) zipped as it is and marked as not executable. `out/source.json` records the image digest or the resolved Maven version, `out/app-artifact.json` the name, size, SHA-256 and kind; the build description shows the name and the SHA-256 prefix. Everything under `out/` is archived with fingerprints, so another job can take `out/artifact/*.jar` with Copy Artifact. Nothing is published to Nexus.

## What the consumer has to do

Run `mvn-rebundle-consumer` (ideally with the consumer's git URL) and apply `out/consumer-patch.md` to the POM: delete the replaced direct dependencies, add the bundle dependency, add the proposed exclusions, declare the test jars it lists and put the Enforcer rule into the parent POM. Then run `mvn clean verify`; a passing Enforcer check means no path brings the original coordinates back.

Then run `mvn-rebundle-verify` on the artifacts built before and after the change. It lists every difference for you to judge, with where it sits:

- in the dependencies: the new artifact must hold exactly one bundle; every jar the bundle declares is looked for in the old artifact (matched by SHA-256, so unidentified jars count too, or by groupId:artifactId when the old build had another version); the embedded coordinates must have disappeared from the dependency set and no third-party jar may be removed, added or at another version; the effective classpath of the third-party jars must be identical, meaning that for every class and resource path the first copy in class-loading order has the same bytes before and after (merged resources are compared as sets of entries, resources that lost copies are reported);
- in the application: the project's own modules rebuilt under another version (CI version stamping) and their changed entries, and the application's own classes and resources (files that always change on a rebuild, such as `build-info.properties`, `classpath.idx` and the archive's own signature, are ignored; entries that embed the build version are marked);
- whether both builds come from the same commit, read from `git.properties` or `build-info.properties`: if they do, a difference in the dependencies means the swap is wrong; if not, the differences may be the code change.

Finally run the existing tests, rebuild the image and smoke-test it.

## Living with a bundle

The bundle freezes its contents at the versions of one build. Families managed by the Spring Boot BOM are therefore tied to the Boot version the application had; the Enforcer rule keeps the consumer on that Boot line, and moving to another line means running `mvn-rebundle` again against the upgraded application, publishing a new bundle version and re-running the verify job. Treat the bundle version as part of the application's upgrade procedure, not as a library with its own lifecycle.

For a change that does not justify a rebuild from the application, such as a hot fix class, a jar that was left out, or a newer version of one library, `mvn-rebundle-rework` changes the published bundle and publishes it under the next version; the consumer job then proposes the matching POM change.

## Running locally

```bash
./run-local.sh bundle --ref /path/app.jar
```

```bash
./run-local.sh bundle --ref /path/app.jar --select "commons-io" --gav com.company.platform:thirdparty-bundle:1.0.0-SNAPSHOT
```

```bash
./run-local.sh consumer --tree dependency-tree.txt --project /path/to/consumer
```

```bash
./run-local.sh verify --old /path/old-app.jar --new /path/new-app.jar
```

```bash
./run-local.sh rework --bundle com.company.platform:thirdparty-bundle:1.0.0 --maven-repo-url https://nexus.example.com/repository/maven-public/ --add-jar fix.jar --add org.apache.commons:commons-lang3:3.14.0
```

```bash
./run-local.sh extract --ref nexus-docker.example.com/team/app:1.2.3 --nexus-hosts nexus-docker.example.com
```

`--ref` takes the same forms as `SOURCE_REF` and is detected the same way. Pass `--maven-repo-url URL` (Maven coordinates and jar identification) and `--nexus-hosts host1,host2` so that references on Nexus use the exported `SOURCE_USERNAME` / `SOURCE_PASSWORD`; images in ECR use the AWS credentials of the environment or the instance role. Without `--select` the bundle mode stops after the inventory. On a machine without Maven add `--skip-maven` to stop after project generation. Publishing needs `export NEXUS_USERNAME NEXUS_PASSWORD` plus `--release-url`, `--snapshot-url` and `--publish`. The consumer mode reads `out/bundle-manifest.json` from a previous bundle run, or takes `--bundle-jar` or `--bundle g:a:v --embedded "..."`. The verify mode exits 0 whenever the comparison ran and prints the differences; `--fail-on dependencies|any` makes findings of that scope exit 1. The rework mode needs `--maven-repo-url` with `SOURCE_USERNAME` / `SOURCE_PASSWORD` exported, takes any number of `--add-jar` files and `--add` coordinates, derives the version unless `--new-version` is given, and publishes like the bundle mode. The extract mode writes the application archive to `out/artifact/` with its `.sha256`; `--name` sets the file name.

Self-test (no Maven needed; a fake Nexus runs locally and the Shade output is simulated from synthetic fixtures):

```bash
python tests/run_selftest.py
```

## Policy

`config/policy.json`:

- `families`: groupId prefixes whose modules are always merged together, with a note shown in the report.
- `signed_jar_note`, `warn_native_libs`, `warn_nested_jars`: wording and switches of the warnings.
- `equivalence.ignore_app_entries`: application-level entries ignored by the equivalence check (build metadata, the archive's own jar signature).

## Known limitations

- The Shade build needs an agent with a JDK and Maven. On a machine without Maven the stages were only self-tested against a simulated Shade output; watch the `Verify bundle` stage on the first real run.
- `createSourcesJar` needs the `-sources.jar` of each dependency in the repository; missing ones only produce a warning.
- When the artifact contains the same groupId:artifactId in several versions or classifiers, select it as `g:a:v` or `g:a:v:classifier`.
- Libraries that introspect their own jar (locating native libraries through `getCodeSource()`, deciding behaviour from `Package.getImplementationVersion()`) may behave differently after merging; only the consumer's tests can catch that.
- A resource that several jars carry under the same path and that a library reads from every jar (`getResources`), such as `reference.conf`, collapses to the first copy after merging. SPI files, the Spring metadata files and the Log4j plugin cache are merged; IDE and documentation metadata (`spring-configuration-metadata.json` and the like) is reported as harmless; everything else is reported by the selection stage and by the verify job so you can decide.
- The Prepare stage prints the mvn-rebundle revision; compare it with the repository when a run does not behave as documented.
- Nexus release repositories reject redeploying an existing version; regenerate under a new version.
- The comparison with the original artifact proves equivalence only when both builds come from the same commit and JDK. With another commit, the differences it lists mix the code change with the effect of the swap; the report says which case applies when the archives carry `git.properties` or `build-info.properties`, and the decision is left to the person approving the push.
- The consumer POM is modified only on a branch created by `APPLY=true`, never on the branch that was checked out, and nothing is merged by the pipeline.
- A test jar declared directly because an exclusion took it away keeps the version the dependency tree showed; when the dependency that brought it is upgraded later, update the test jar with it.
- A direct dependency of a build plugin that is embedded in the bundle cannot be removed from the plugin's class realm: Maven has no exclusion at the plugin level and re-declaring the coordinate still resolves it. The bundle is placed ahead of it, so classes load from the bundle, but the original jar is still downloaded and listed by `dependency:resolve-plugins`; `plugins.direct_hits=skip` in the policy leaves such plugins alone.
- The rework job replaces a component with the help of the jar Nexus has under the component's coordinates. An entry whose bytes differ from the bundle's copy stays (it came from another component), and a component under synthetic coordinates, or whose jar Nexus no longer has, is replaced only where the new jar carries the same entry; the plan warns in both cases, and the published `BUNDLE-CONTENTS.txt` still lists the SHA-256 of every component for a check by hand. Removing a component is a rebuild with `mvn-rebundle`.
- A dependency an added jar's POM declares enters the published POM of the reworked bundle (the original application never had it); the finalize step and the verification report it, the reviewer decides whether the consumer should take it.
- The uploaded jar needs the File Parameter plugin. Without it, remove the `ADD_JAR` parameter and the `Uploaded jar` stage from `jenkins/Jenkinsfile.rework` and add jars by coordinates only.
