// dep-bundle-pipeline —— 从运行中的 Spring Boot 制品（镜像或 jar）提取指定第三方依赖，
// 用 maven-shade-plugin 合并成一个新坐标的 bundle，校验后发布到 Nexus，并生成消费方 POM 修改建议。
//
// Agent 要求（Linux）：JDK 17+、Maven 3.6+、Python 3.8+；镜像输入还需要 docker（可访问 daemon）或 crane。
// 站点相关配置只在下面的 environment 块里改一次。
//
// Job 配置：Pipeline script from SCM，Script Path = Jenkinsfile。第一次运行只是注册参数，之后用 "Build with Parameters"。

def withNexusCredentials(Closure body) {
    if (env.NEXUS_CREDENTIALS_ID?.trim()) {
        withCredentials([usernamePassword(credentialsId: env.NEXUS_CREDENTIALS_ID,
                                          usernameVariable: 'NEXUS_USERNAME',
                                          passwordVariable: 'NEXUS_PASSWORD')]) {
            body()
        }
    } else {
        body()
    }
}

pipeline {
    agent any            // 或 agent { label 'maven-docker' }

    options {
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timeout(time: 60, unit: 'MINUTES')
    }

    parameters {
        choice(name: 'SOURCE_TYPE', choices: ['image', 'jar'],
               description: 'image：容器镜像；jar：可执行 jar/war 的 http(s) 地址或 agent 本地路径')
        string(name: 'SOURCE_REF', defaultValue: '',
               description: '镜像引用（registry.example.com/team/app:1.2.3）或 jar 地址')
        string(name: 'APP_PATH', defaultValue: '/app/app.jar',
               description: '镜像内应用路径：fat jar 文件；或解包目录（buildpacks 镜像为 /workspace，Jib 镜像为 /app）')
        string(name: 'IMAGE_PLATFORM', defaultValue: '', description: '镜像平台，例如 linux/amd64；留空用默认')
        choice(name: 'IMAGE_TOOL', choices: ['auto', 'docker', 'crane'], description: '镜像提取工具')
        text(name: 'SELECT', defaultValue: 'commons-io:commons-io',
             description: '要合并的依赖，一行一个：artifactId | groupId:artifactId | groupId:artifactId:version')
        string(name: 'OUT_GROUP_ID', defaultValue: 'com.company.platform', description: 'bundle 的 groupId')
        string(name: 'OUT_ARTIFACT_ID', defaultValue: 'thirdparty-bundle', description: 'bundle 的 artifactId')
        string(name: 'OUT_VERSION', defaultValue: '1.0.0-SNAPSHOT', description: 'bundle 的 version；-SNAPSHOT 发布到 snapshot 仓库')
        choice(name: 'POLICY_MODE', choices: ['strict', 'warn'],
               description: 'strict：策略拒绝（家族依赖、签名 jar 等）即失败；warn：降级为警告继续。坐标无法识别、同路径 class 字节不同等硬性错误在两种模式下都失败')
        booleanParam(name: 'CREATE_SOURCES_JAR', defaultValue: true, description: '同时生成 -sources.jar，IDE 里能看到源码')
        booleanParam(name: 'PUBLISH', defaultValue: false, description: '校验通过后 mvn deploy 到 Nexus；false 只构建与校验')
        string(name: 'CONSUMER_GIT_URL', defaultValue: '',
               description: '可选：消费方工程 git 地址。提供后会跑 dependency:tree，生成逐路径的 exclusion 建议')
        string(name: 'CONSUMER_GIT_REF', defaultValue: 'main', description: '消费方分支、tag 或提交')
        string(name: 'CONSUMER_MAVEN_ARGS', defaultValue: '', description: '消费方 dependency:tree 的附加参数，例如 -Pprod')
    }

    environment {
        // ---- 站点相关，改一次 ------------------------------------------------------------
        NEXUS_RELEASE_URL    = 'https://nexus.example.com/repository/maven-releases/'
        NEXUS_SNAPSHOT_URL   = 'https://nexus.example.com/repository/maven-snapshots/'
        NEXUS_CREDENTIALS_ID = 'nexus-deploy'   // Jenkins 里的 username/password 凭据 ID；留空则匿名（无法发布）
        MAVEN_MIRROR_URL     = ''               // 公司 Maven group 仓库，例如 https://nexus.example.com/repository/maven-public/ ；留空直接用 Maven Central
        GIT_CREDENTIALS_ID   = ''               // 可选：检出 CONSUMER_GIT_URL 用的凭据 ID
        // -----------------------------------------------------------------------------------
        PY        = 'python3'
        MVN_FLAGS = '-B -ntp -s work/settings.xml'
    }

    stages {
        stage('Prepare') {
            steps {
                sh '''
                    rm -rf work out && mkdir -p work out
                    "$PY" --version
                    java -version
                    mvn -v
                '''
                writeFile file: 'work/select.txt', text: params.SELECT
                sh '"$PY" scripts/gen_maven_settings.py --server-id nexus --mirror-url "$MAVEN_MIRROR_URL" --out work/settings.xml'
            }
        }

        stage('Fetch source') {
            steps {
                sh '''
                    "$PY" scripts/extract_source.py --type "$SOURCE_TYPE" --ref "$SOURCE_REF" --path "$APP_PATH" \
                        --image-tool "$IMAGE_TOOL" --platform "$IMAGE_PLATFORM" --out work/app --meta out/source.json
                '''
            }
        }

        stage('Inspect application') {
            steps {
                sh '"$PY" scripts/inspect_app.py --source out/source.json --out out/inventory.json'
            }
        }

        stage('Resolve selection & policy') {
            steps {
                sh '''
                    "$PY" scripts/resolve_selection.py --inventory out/inventory.json --select-file work/select.txt \
                        --policy config/policy.json --policy-mode "$POLICY_MODE" --out out/selection.json
                '''
            }
        }

        stage('Generate bundle project') {
            steps {
                sh '''
                    "$PY" scripts/gen_bundle_project.py --selection out/selection.json --source out/source.json \
                        --group-id "$OUT_GROUP_ID" --artifact-id "$OUT_ARTIFACT_ID" --version "$OUT_VERSION" \
                        --release-url "$NEXUS_RELEASE_URL" --snapshot-url "$NEXUS_SNAPSHOT_URL" --server-id nexus \
                        --sources-jar "$CREATE_SOURCES_JAR" --out work/bundle --manifest-out out/bundle-manifest.json
                '''
            }
        }

        stage('Build bundle') {
            steps {
                script {
                    withNexusCredentials {
                        sh 'mvn $MVN_FLAGS -f work/bundle/pom.xml clean verify'
                    }
                }
            }
        }

        stage('Verify bundle') {
            steps {
                sh '"$PY" scripts/verify_bundle.py --manifest out/bundle-manifest.json --out out/bundle-verification.json'
            }
        }

        stage('Consumer dependency tree') {
            when { expression { params.CONSUMER_GIT_URL?.trim() } }
            steps {
                dir('work/consumer') {
                    checkout([$class: 'GitSCM',
                              branches: [[name: params.CONSUMER_GIT_REF]],
                              userRemoteConfigs: [[url: params.CONSUMER_GIT_URL,
                                                   credentialsId: (env.GIT_CREDENTIALS_ID?.trim() ?: null)]]])
                    script {
                        withNexusCredentials {
                            sh '''
                                mvn -B -ntp -s "$WORKSPACE/work/settings.xml" \
                                    org.apache.maven.plugins:maven-dependency-plugin:3.7.1:tree \
                                    -Dverbose -DoutputType=text -DappendOutput=true \
                                    -DoutputFile="$WORKSPACE/out/consumer-dependency-tree.txt" $CONSUMER_MAVEN_ARGS
                            '''
                        }
                    }
                }
            }
        }

        stage('Consumer patch proposal') {
            steps {
                sh '''
                    TREE=""
                    if [ -f out/consumer-dependency-tree.txt ]; then TREE="--tree out/consumer-dependency-tree.txt"; fi
                    "$PY" scripts/gen_consumer_patch.py --manifest out/bundle-manifest.json $TREE \
                        --out out/consumer-patch.md --out-json out/consumer-patch.json
                '''
            }
        }

        stage('Publish to Nexus') {
            when { expression { params.PUBLISH } }
            steps {
                script {
                    if (!env.NEXUS_CREDENTIALS_ID?.trim()) {
                        error 'PUBLISH=true 需要在 environment 里配置 NEXUS_CREDENTIALS_ID'
                    }
                    withNexusCredentials {
                        // 可重复构建：deploy 重新打出的 jar 与刚校验过的字节一致
                        sh 'mvn $MVN_FLAGS -f work/bundle/pom.xml deploy'
                    }
                }
            }
        }
    }

    post {
        always {
            sh '"$PY" scripts/write_summary.py --out-dir out --published "$PUBLISH" || true'
            archiveArtifacts artifacts: 'out/**, work/bundle/pom.xml, work/bundle/target/*.jar, work/bundle/target/dependency-reduced-pom.xml',
                             allowEmptyArchive: true, fingerprint: true
        }
    }
}
