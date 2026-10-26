// mvn-rebundle: extract third-party dependencies from a running Spring Boot artifact (container image,
// Nexus coordinates, URL or jar), merge them with maven-shade-plugin into a bundle under new
// coordinates, verify the bundle byte for byte against the original jars and, on request, publish
// exactly those bytes to Nexus.
//
// Everything related to a selected jar (same family, same groupId and version, declared dependencies)
// is merged with it automatically; nothing is refused for policy reasons.
//
// Companion jobs: jenkins/Jenkinsfile.consumer (POM change proposal for a consuming project) and
// jenkins/Jenkinsfile.verify (equivalence check of the application before and after the change).
//
// Agent requirements (Linux): JDK 17+, Maven 3.6+, Python 3.8+; docker (with a reachable daemon) or
// crane for images; the aws CLI for images in ECR. Site-specific settings live in `environment`.
//
// Job setup: "Pipeline script from SCM", Script Path = Jenkinsfile. The first run only registers
// the parameters; use "Build with Parameters" afterwards.

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

// Credentials needed to read from the source; `auth` is what `extract_source.py --detect` printed.
def withSourceCredentials(String auth, Closure body) {
    if (auth == 'ecr' && env.AWS_CREDENTIALS_ID?.trim()) {
        withCredentials([aws(credentialsId: env.AWS_CREDENTIALS_ID,
                             accessKeyVariable: 'AWS_ACCESS_KEY_ID', secretKeyVariable: 'AWS_SECRET_ACCESS_KEY')]) {
            body()
        }
    } else if (auth == 'nexus' && env.SOURCE_CREDENTIALS_ID?.trim()) {
        withCredentials([usernamePassword(credentialsId: env.SOURCE_CREDENTIALS_ID,
                                          usernameVariable: 'SOURCE_USERNAME', passwordVariable: 'SOURCE_PASSWORD')]) {
            body()
        }
    } else {
        body()
    }
}

pipeline {
    agent any            // or: agent { label 'maven-docker' }

    options {
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timeout(time: 60, unit: 'MINUTES')
    }

    parameters {
        string(name: 'SOURCE_REF', defaultValue: '',
               description: 'Where the application comes from: an image reference (append #/path when the application is not at DEFAULT_APP_PATH), Maven coordinates groupId:artifactId:version[:packaging[:classifier]], an http(s) URL, or a path on the agent. The kind of source and the login it needs are detected from the reference')
        text(name: 'SELECT', defaultValue: '',
             description: 'Dependencies to merge, one per line: artifactId | groupId:artifactId | groupId:artifactId:version | * for every jar; -name excludes one. Everything related (same family, same groupId and version, declared dependencies) is added automatically. Leave empty to only list the inventory')
        string(name: 'BUNDLE_ARTIFACT_ID', defaultValue: 'thirdparty-bundle', description: 'artifactId of the bundle; the groupId is BUNDLE_GROUP_ID in the environment block')
        string(name: 'BUNDLE_VERSION', defaultValue: '1.0.0-SNAPSHOT', description: 'version of the bundle; -SNAPSHOT goes to the snapshot repository')
        booleanParam(name: 'PUBLISH', defaultValue: false,
                     description: 'Upload the verified jar, its dependency-reduced POM and the sources jar to Nexus; false builds and verifies only')
    }

    environment {
        // ---- site specific: edit once -------------------------------------------------------
        NEXUS_RELEASE_URL     = 'https://nexus.example.com/repository/maven-releases/'
        NEXUS_SNAPSHOT_URL    = 'https://nexus.example.com/repository/maven-snapshots/'
        NEXUS_MAVEN_REPO_URL  = 'https://nexus.example.com/repository/maven-public/'   // resolves Maven coordinates and identifies jars without pom.properties
        NEXUS_DOCKER_REGISTRY = 'nexus-docker.example.com'   // host[:port] of the Nexus Docker registry; images from it log in with SOURCE_CREDENTIALS_ID
        NEXUS_CREDENTIALS_ID  = 'nexus-deploy'   // username/password credential for publishing; empty = anonymous (cannot publish)
        SOURCE_CREDENTIALS_ID = 'nexus-deploy'   // Nexus credential for reading images, URLs and coordinates hosted on Nexus; may equal NEXUS_CREDENTIALS_ID
        AWS_CREDENTIALS_ID    = ''               // images in ECR: AWS credential ID (CloudBees AWS Credentials plugin); empty = the agent's instance role
        MAVEN_MIRROR_URL      = ''               // Maven mirror for the bundle build, e.g. the group repository above; empty = Maven Central
        BUNDLE_GROUP_ID       = 'com.company.platform'
        DEFAULT_APP_PATH      = '/app/app.jar'   // application path inside images (fat jar file, or /workspace for buildpacks, /app for Jib)
        IMAGE_PLATFORM        = ''               // e.g. linux/amd64 for multi-arch images; empty = default
        IMAGE_TOOL            = 'auto'           // auto | docker | crane
        CREATE_SOURCES_JAR    = 'true'
        // -----------------------------------------------------------------------------------
        NEXUS_HOSTS = "${NEXUS_DOCKER_REGISTRY},${NEXUS_MAVEN_REPO_URL},${NEXUS_RELEASE_URL},${NEXUS_SNAPSHOT_URL}"
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
                script {
                    // kind, auth and host are detected from the reference; bind only the credentials that are needed
                    def detected = sh(returnStdout: true,
                                      script: '"$PY" scripts/extract_source.py --detect --ref "$SOURCE_REF" --nexus-hosts "$NEXUS_HOSTS"').trim()
                    echo "Source detected: ${detected}"
                    withSourceCredentials(detected.tokenize()[1]) {
                        sh '''
                            "$PY" scripts/extract_source.py --ref "$SOURCE_REF" --path "$DEFAULT_APP_PATH" --nexus-hosts "$NEXUS_HOSTS" \
                                --image-tool "$IMAGE_TOOL" --platform "$IMAGE_PLATFORM" --maven-repo-url "$NEXUS_MAVEN_REPO_URL" \
                                --out work/app --meta out/source.json
                        '''
                    }
                }
            }
        }

        stage('Inspect application') {
            steps {
                script {
                    // the Nexus credential lets the inventory identify jars without pom.properties through the repository
                    withSourceCredentials('nexus') {
                        sh '"$PY" scripts/inspect_app.py --source out/source.json --repo-url "$NEXUS_MAVEN_REPO_URL" --out out/inventory.json'
                    }
                }
            }
        }

        stage('Bundle') {
            when { expression { params.SELECT?.trim() } }
            stages {
                stage('Resolve selection') {
                    steps {
                        sh '''
                            "$PY" scripts/resolve_selection.py --inventory out/inventory.json --select-file work/select.txt \
                                --policy config/policy.json --out out/selection.json
                        '''
                    }
                }

                stage('Generate bundle project') {
                    steps {
                        sh '''
                            "$PY" scripts/gen_bundle_project.py --selection out/selection.json --source out/source.json \
                                --group-id "$BUNDLE_GROUP_ID" --artifact-id "$BUNDLE_ARTIFACT_ID" --version "$BUNDLE_VERSION" \
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
            }
        }

        stage('Publish to Nexus') {
            when {
                allOf {
                    expression { params.PUBLISH }
                    expression { params.SELECT?.trim() }
                }
            }
            steps {
                script {
                    if (!env.NEXUS_CREDENTIALS_ID?.trim()) {
                        error 'PUBLISH=true requires NEXUS_CREDENTIALS_ID in the environment block'
                    }
                    withNexusCredentials {
                        // deploy-file uploads the jar that was just verified; nothing is rebuilt
                        sh '''
                            "$PY" scripts/publish_bundle.py --manifest out/bundle-manifest.json \
                                --verification out/bundle-verification.json --settings work/settings.xml \
                                --release-url "$NEXUS_RELEASE_URL" --snapshot-url "$NEXUS_SNAPSHOT_URL" --server-id nexus \
                                --out out/publish.json
                        '''
                    }
                }
            }
        }
    }

    post {
        always {
            sh '"$PY" scripts/write_summary.py --out-dir out || true'
            archiveArtifacts artifacts: 'out/**, work/bundle/pom.xml, work/bundle/target/*.jar, work/bundle/target/dependency-reduced-pom.xml',
                             allowEmptyArchive: true, fingerprint: true
        }
    }
}
