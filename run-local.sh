#!/usr/bin/env bash
# Run the same stages as the Jenkins jobs on a workstation (Linux, macOS or Git Bash on Windows).
#
#   ./run-local.sh bundle --ref REF [--select "commons-io"] [--gav com.company.platform:thirdparty-bundle:1.0.0-SNAPSHOT] \
#        [--path /app/app.jar] [--maven-repo-url URL] [--nexus-hosts host1,host2] [--aws-region R] [--type TYPE] \
#        [--image-tool auto|docker|crane] [--platform linux/amd64] [--no-lookup] [--no-expand] [--no-sources] \
#        [--mirror-url URL] [--release-url URL --snapshot-url URL --publish] [--skip-maven]
#        (no --select: inventory only; --gav is required with --select)
#
#   ./run-local.sh consumer [--manifest out/bundle-manifest.json | --bundle-jar X.jar [--reduced-pom X.pom]
#        | --bundle g:a:v --embedded "g:a:v,g:a:v"] [--tree dependency-tree.txt] [--project DIR [--apply]]
#        [--plugin-tree plugin-trees.txt --plugins consumer-plugins.json]
#        (--project with a tree: edits the consumer POMs; without --apply only the diff is produced;
#         --plugin-tree/--plugins: build plugins, from plugin_trees.py and its reactor's dependency:tree)
#
#   ./run-local.sh verify --old REF --new REF [--bundle-artifact-id ID] [--fail-on dependencies|any] [--path /app/app.jar] \
#        [--maven-repo-url URL] [--nexus-hosts host1,host2] [--aws-region R]
#        (the comparison only reports; --fail-on makes findings of that scope exit 1)
#
#   ./run-local.sh rework --bundle g:a:v --maven-repo-url URL [--add-jar X.jar]... [--add g:a:v[:classifier]]... [--new-version V] \
#        [--no-sources] [--mirror-url URL] [--release-url URL --snapshot-url URL --publish] [--skip-maven]
#        (adds jars to a published bundle, or replaces embedded components by newer versions, and republishes it)
#
# REF is detected like SOURCE_REF in Jenkins: an image reference (append #/path for the application inside
# it), Maven coordinates g:a:v[:packaging[:classifier]], an http(s) URL, or a local file / directory.
# Credentials come from the environment: SOURCE_USERNAME / SOURCE_PASSWORD for the Nexus hosts, the usual
# AWS variables or an instance role for ECR, NEXUS_USERNAME / NEXUS_PASSWORD for publishing.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python

mode=${1:-bundle}
[ $# -gt 0 ] && shift

usage() { sed -n '2,27p' "$0"; exit 2; }

extract() {  # <ref> <path> <out dir> <meta file>
    "$PY" scripts/extract_source.py --ref "$1" --path "$2" --type "$type" --nexus-hosts "$nexus_hosts" \
        --image-tool "$image_tool" --platform "$platform" --aws-region "$aws_region" --maven-repo-url "$maven_repo_url" \
        --out "$3" --meta "$4"
}

if [ "$mode" = "bundle" ]; then
    ref="" path=/app/app.jar type=auto image_tool=auto platform="" aws_region="" maven_repo_url="" nexus_hosts=""
    select="" gav="" sources=true mirror_url="" release_url="" snapshot_url="" publish=false skip_maven=false
    lookup=() expand=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref) ref=$2; shift 2;;
            --path) path=$2; shift 2;;
            --type) type=$2; shift 2;;
            --image-tool) image_tool=$2; shift 2;;
            --platform) platform=$2; shift 2;;
            --aws-region) aws_region=$2; shift 2;;
            --maven-repo-url) maven_repo_url=$2; shift 2;;
            --nexus-hosts) nexus_hosts=$2; shift 2;;
            --select) select=$2; shift 2;;
            --gav) gav=$2; shift 2;;
            --no-lookup) lookup=(--no-lookup); shift;;
            --no-expand) expand=(--no-expand); shift;;
            --no-sources) sources=false; shift;;
            --mirror-url) mirror_url=$2; shift 2;;
            --release-url) release_url=$2; shift 2;;
            --snapshot-url) snapshot_url=$2; shift 2;;
            --publish) publish=true; shift;;
            --skip-maven) skip_maven=true; shift;;
            *) echo "unknown option: $1"; usage;;
        esac
    done
    [ -n "$ref" ] || usage
    if [ -n "$select" ]; then
        [ -n "$gav" ] || { echo "--gav groupId:artifactId:version is required with --select"; exit 2; }
        IFS=: read -r g a v <<<"$gav"
        [ -n "${v:-}" ] || { echo "--gav must be groupId:artifactId:version"; exit 2; }
    fi

    rm -rf work out && mkdir -p work out
    "$PY" scripts/gen_maven_settings.py --server-id nexus --mirror-url "$mirror_url" --out work/settings.xml
    extract "$ref" "$path" work/app out/source.json
    "$PY" scripts/inspect_app.py --source out/source.json --repo-url "$maven_repo_url" "${lookup[@]}" --out out/inventory.json
    if [ -z "$select" ]; then
        "$PY" scripts/write_summary.py --out-dir out
        echo "inventory only (no --select): see out/inventory.json"
        exit 0
    fi
    printf '%s\n' "$select" > work/select.txt
    "$PY" scripts/resolve_selection.py --inventory out/inventory.json --select-file work/select.txt \
        --policy config/policy.json "${expand[@]}" --out out/selection.json
    "$PY" scripts/gen_bundle_project.py --selection out/selection.json --source out/source.json \
        --group-id "$g" --artifact-id "$a" --version "$v" --release-url "$release_url" --snapshot-url "$snapshot_url" \
        --server-id nexus --sources-jar "$sources" --out work/bundle --manifest-out out/bundle-manifest.json
    if [ "$skip_maven" = false ]; then
        local_repo="$PWD/work/m2"   # job-local repository: components are installed here from the application's bytes
        "$PY" scripts/install_components.py --manifest out/bundle-manifest.json --settings work/settings.xml \
            --repo-url "$maven_repo_url" --local-repo "$local_repo"
        mvn -B -ntp -s work/settings.xml -Dmaven.repo.local="$local_repo" -f work/bundle/pom.xml clean verify
        "$PY" scripts/finalize_bundle_pom.py --manifest out/bundle-manifest.json --report out/bundle-pom.json
        "$PY" scripts/verify_bundle.py --manifest out/bundle-manifest.json --out out/bundle-verification.json
        if [ "$publish" = true ]; then
            "$PY" scripts/publish_bundle.py --manifest out/bundle-manifest.json --verification out/bundle-verification.json \
                --settings work/settings.xml --release-url "$release_url" --snapshot-url "$snapshot_url" --server-id nexus \
                --local-repo "$local_repo" --out out/publish.json
        fi
    else
        echo "skipping Maven build, bundle verification and publishing (--skip-maven)"
    fi
    "$PY" scripts/write_summary.py --out-dir out
    echo "done: see out/SUMMARY.md"

elif [ "$mode" = "consumer" ]; then
    manifest=out/bundle-manifest.json bundle_jar="" reduced_pom="" bundle="" embedded="" tree="" project="" apply=false plugin_tree="" plugins=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --manifest) manifest=$2; shift 2;;
            --bundle-jar) bundle_jar=$2; shift 2;;
            --reduced-pom) reduced_pom=$2; shift 2;;
            --bundle) bundle=$2; shift 2;;
            --embedded) embedded=$2; shift 2;;
            --tree) tree=$2; shift 2;;
            --plugin-tree) plugin_tree=$2; shift 2;;
            --plugins) plugins=$2; shift 2;;
            --project) project=$2; shift 2;;
            --apply) apply=true; shift;;
            *) echo "unknown option: $1"; usage;;
        esac
    done
    mkdir -p out
    args=()
    [ -n "$tree" ] && args+=(--tree "$tree")
    [ -n "$plugin_tree" ] && args+=(--plugin-tree "$plugin_tree")
    [ -n "$plugins" ] && args+=(--plugins "$plugins")
    [ -n "$reduced_pom" ] && args+=(--reduced-pom "$reduced_pom")
    if [ -n "$bundle_jar" ]; then
        "$PY" scripts/gen_consumer_patch.py --bundle-jar "$bundle_jar" "${args[@]}" --out out/consumer-patch.md --out-json out/consumer-patch.json
    elif [ -n "$bundle" ]; then
        "$PY" scripts/gen_consumer_patch.py --bundle "$bundle" --embedded "$embedded" "${args[@]}" --out out/consumer-patch.md --out-json out/consumer-patch.json
    else
        [ -f "$manifest" ] || { echo "manifest not found: $manifest (run the bundle mode first, or pass --bundle-jar / --bundle)"; exit 2; }
        "$PY" scripts/gen_consumer_patch.py --manifest "$manifest" "${args[@]}" --out out/consumer-patch.md --out-json out/consumer-patch.json
    fi
    if [ -n "$project" ]; then
        dry=(--dry-run)
        [ "$apply" = true ] && dry=()
        "$PY" scripts/apply_consumer_patch.py --patch out/consumer-patch.json --project "$project" "${dry[@]}" \
            --out out/consumer-apply.json --diff out/consumer-apply.diff
        echo "diff in out/consumer-apply.diff$([ "$apply" = true ] || echo ' (dry run: pass --apply to edit the POMs)')"
    fi
    echo "done: see out/consumer-patch.md"

elif [ "$mode" = "verify" ]; then
    old="" new="" bundle_artifact_id="" path=/app/app.jar type=auto image_tool=auto platform="" aws_region="" maven_repo_url="" nexus_hosts=""
    fail_on=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --old) old=$2; shift 2;;
            --new) new=$2; shift 2;;
            --bundle-artifact-id) bundle_artifact_id=$2; shift 2;;
            --fail-on) fail_on=(--fail-on "$2"); shift 2;;
            --path) path=$2; shift 2;;
            --image-tool) image_tool=$2; shift 2;;
            --platform) platform=$2; shift 2;;
            --aws-region) aws_region=$2; shift 2;;
            --maven-repo-url) maven_repo_url=$2; shift 2;;
            --nexus-hosts) nexus_hosts=$2; shift 2;;
            *) echo "unknown option: $1"; usage;;
        esac
    done
    [ -n "$old" ] && [ -n "$new" ] || usage
    rm -rf work out && mkdir -p work out
    extract "$old" "$path" work/old out/old-source.json
    extract "$new" "$path" work/new out/new-source.json
    "$PY" scripts/verify_app_equivalence.py --old-source out/old-source.json --new-source out/new-source.json \
        --bundle-artifact-id "$bundle_artifact_id" --policy config/policy.json "${fail_on[@]}" --out out/equivalence.json
    "$PY" scripts/write_summary.py --out-dir out
    echo "done: see out/SUMMARY.md"

elif [ "$mode" = "rework" ]; then
    bundle="" maven_repo_url="" new_version="" sources=true mirror_url="" release_url="" snapshot_url="" publish=false skip_maven=false
    add_jars=() adds=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --bundle) bundle=$2; shift 2;;
            --maven-repo-url) maven_repo_url=$2; shift 2;;
            --add-jar) add_jars+=(--add-jar "$2"); shift 2;;
            --add) adds+=(--add "$2"); shift 2;;
            --new-version) new_version=$2; shift 2;;
            --no-sources) sources=false; shift;;
            --mirror-url) mirror_url=$2; shift 2;;
            --release-url) release_url=$2; shift 2;;
            --snapshot-url) snapshot_url=$2; shift 2;;
            --publish) publish=true; shift;;
            --skip-maven) skip_maven=true; shift;;
            *) echo "unknown option: $1"; usage;;
        esac
    done
    [ -n "$bundle" ] && [ -n "$maven_repo_url" ] || usage
    rm -rf work out && mkdir -p work out
    "$PY" scripts/gen_maven_settings.py --server-id nexus --mirror-url "$mirror_url" --out work/settings.xml
    "$PY" scripts/fetch_bundle.py --bundle "$bundle" "${adds[@]}" --repo-url "$maven_repo_url" \
        --out work/bundle-in --added-out work/added --meta out/rework-bundle.json
    "$PY" scripts/rework_bundle.py --bundle-meta out/rework-bundle.json "${add_jars[@]}" --new-version "$new_version" \
        --repo-url "$maven_repo_url" --release-url "$release_url" --snapshot-url "$snapshot_url" --server-id nexus \
        --sources-jar "$sources" --out work/rework --manifest-out out/bundle-manifest.json --plan-out out/rework-plan.json
    if [ "$skip_maven" = false ]; then
        local_repo="$PWD/work/m2"   # job-local repository: the inputs are installed here from the fetched bytes
        "$PY" scripts/install_components.py --manifest out/bundle-manifest.json --settings work/settings.xml \
            --repo-url "$maven_repo_url" --local-repo "$local_repo"
        mvn -B -ntp -s work/settings.xml -Dmaven.repo.local="$local_repo" -f work/rework/pom.xml clean verify
        "$PY" scripts/finalize_bundle_pom.py --manifest out/bundle-manifest.json --report out/bundle-pom.json
        "$PY" scripts/verify_bundle.py --manifest out/bundle-manifest.json --out out/bundle-verification.json
        if [ "$publish" = true ]; then
            "$PY" scripts/publish_bundle.py --manifest out/bundle-manifest.json --verification out/bundle-verification.json \
                --settings work/settings.xml --release-url "$release_url" --snapshot-url "$snapshot_url" --server-id nexus \
                --local-repo "$local_repo" --out out/publish.json
        fi
    else
        echo "skipping Maven build, bundle verification and publishing (--skip-maven)"
    fi
    "$PY" scripts/write_summary.py --out-dir out
    echo "done: see out/SUMMARY.md"
else
    usage
fi
