// mvn-rebundle: extract selected third-party dependencies from a running Spring Boot artifact
// (container image or jar), merge them with maven-shade-plugin into a bundle under new coordinates,
// verify the bundle byte for byte against the original jars and, on request, publish exactly those
// bytes to Nexus.
//
// Companion jobs: jenkins/Jenkinsfile.consumer (POM change proposal for a consuming project) and
// jenkins/Jenkinsfile.verify (equivalence check of the application before and after the change).
//
// Agent requirements (Linux): JDK 17+, Maven 3.6+, Python 3.8+; for image sources either docker
// (with a reachable daemon) or crane. Site-specific settings live in the `environment` block below.
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

pipeline {
    agent any            // or: agent { label 'maven-docker' }

    options {
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timeout(time: 60, unit: 'MINUTES')
    }

    parameters {
        choice(name: 'SOURCE_TYPE', choices: ['image', 'jar'],
               description: 'image: container image; jar: http(s) URL or agent-local path of an executable jar/war')
        string(name: 'SOURCE_REF', defaultValue: '',
               description: 'Image reference (registry.example.com/team/app:1.2.3) or jar location')
        string(name: 'APP_PATH', defaultValue: '/app/app.jar',
               description: 'Application path inside the image: a fat jar file, or an exploded directory (/workspace for buildpacks images, /app for Jib images)')
        string(name: 'IMAGE_PLATFORM', defaultValue: '', description: 'Image platform, e.g. linux/amd64; empty uses the default')
        choice(name: 'IMAGE_TOOL', choices: ['auto', 'docker', 'crane'], description: 'Tool used to read the image')
        booleanParam(name: 'INVENTORY_ONLY', defaultValue: false,
                     description: 'Stop after the inventory: lists every dependency jar with its coordinates so you can decide what to select')
        text(name: 'SELECT', defaultValue: 'commons-io:commons-io',
             description: 'Dependencies to merge, one per line: artifactId | groupId:artifactId | groupId:artifactId:version')
        string(name: 'OUT_GROUP_ID', defaultValue: 'com.company.platform', description: 'groupId of the bundle')
        string(name: 'OUT_ARTIFACT_ID', defaultValue: 'thirdparty-bundle', description: 'artifactId of the bundle')
        string(name: 'OUT_VERSION', defaultValue: '1.0.0-SNAPSHOT', description: 'version of the bundle; -SNAPSHOT goes to the snapshot repository')
        choice(name: 'POLICY_MODE', choices: ['strict', 'warn'],
               description: 'strict: policy denials (version-aligned families, signed jars, ...) fail the build; warn: they become warnings. Hard errors (unidentifiable coordinates, same class path with different bytes) fail in both modes')
        booleanParam(name: 'CREATE_SOURCES_JAR', defaultValue: true, description: 'Also build a -sources.jar so IDEs show the merged sources')
        booleanParam(name: 'PUBLISH', defaultValue: false,
                     description: 'Upload the verified jar, its dependency-reduced POM and the sources jar to Nexus with deploy-file; false builds and verifies only')
    }

    environment {
        // ---- site specific: edit once -------------------------------------------------------
        NEXUS_RELEASE_URL    = 'https://nexus.example.com/repository/maven-releases/'
        NEXUS_SNAPSHOT_URL   = 'https://nexus.example.com/repository/maven-snapshots/'
        NEXUS_CREDENTIALS_ID = 'nexus-deploy'   // Jenkins username/password credential ID; empty = anonymous (cannot publish)
        MAVEN_MIRROR_URL     = ''               // company Maven group repository, e.g. https://nexus.example.com/repository/maven-public/ ; empty = Maven Central
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

        stage('Bundle') {
            when { expression { !params.INVENTORY_ONLY } }
            stages {
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
            }
        }

        stage('Publish to Nexus') {
            when {
                allOf {
                    expression { params.PUBLISH }
                    expression { !params.INVENTORY_ONLY }
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
