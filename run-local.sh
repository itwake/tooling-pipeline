#!/usr/bin/env bash
# Run the same stages as the Jenkinsfiles on a workstation (Linux, macOS or Git Bash on Windows).
#
#   ./run-local.sh bundle --type jar --ref /path/app.jar --select "commons-io" \
#        --gav com.company.platform:thirdparty-bundle:1.0.0-SNAPSHOT \
#        [--path /app/app.jar] [--image-tool auto|docker|crane] [--platform linux/amd64] \
#        [--policy-mode strict|warn] [--no-sources] [--mirror-url URL] \
#        [--release-url URL --snapshot-url URL --publish] [--tree dependency-tree.txt] [--skip-maven]
#
#   ./run-local.sh verify --old /path/old-app.jar --new /path/new-app.jar [--bundle-artifact-id thirdparty-bundle]
#        (--old / --new accept a jar, an exploded directory, or image:<ref>[#<path>] when docker/crane is available)
#
# Nexus credentials for publishing: export NEXUS_USERNAME / NEXUS_PASSWORD before running.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python

mode=${1:-bundle}
[ $# -gt 0 ] && shift

usage() { sed -n '2,15p' "$0"; exit 2; }

if [ "$mode" = "bundle" ]; then
    type=jar ref="" path=/app/app.jar image_tool=auto platform="" select="" gav="" policy_mode=strict
    sources=true mirror_url="" release_url="" snapshot_url="" publish=false tree="" skip_maven=false
    while [ $# -gt 0 ]; do
        case "$1" in
            --type) type=$2; shift 2;;
            --ref) ref=$2; shift 2;;
            --path) path=$2; shift 2;;
            --image-tool) image_tool=$2; shift 2;;
            --platform) platform=$2; shift 2;;
            --select) select=$2; shift 2;;
            --gav) gav=$2; shift 2;;
            --policy-mode) policy_mode=$2; shift 2;;
            --no-sources) sources=false; shift;;
            --mirror-url) mirror_url=$2; shift 2;;
            --release-url) release_url=$2; shift 2;;
            --snapshot-url) snapshot_url=$2; shift 2;;
            --publish) publish=true; shift;;
            --tree) tree=$2; shift 2;;
            --skip-maven) skip_maven=true; shift;;
            *) echo "unknown option: $1"; usage;;
        esac
    done
    [ -n "$ref" ] && [ -n "$select" ] && [ -n "$gav" ] || usage
    IFS=: read -r g a v <<<"$gav"
    [ -n "${v:-}" ] || { echo "--gav must be groupId:artifactId:version"; exit 2; }

    rm -rf work out && mkdir -p work out
    printf '%s\n' "$select" > work/select.txt
    "$PY" scripts/gen_maven_settings.py --server-id nexus --mirror-url "$mirror_url" --out work/settings.xml
    "$PY" scripts/extract_source.py --type "$type" --ref "$ref" --path "$path" --image-tool "$image_tool" \
        --platform "$platform" --out work/app --meta out/source.json
    "$PY" scripts/inspect_app.py --source out/source.json --out out/inventory.json
    "$PY" scripts/resolve_selection.py --inventory out/inventory.json --select-file work/select.txt \
        --policy config/policy.json --policy-mode "$policy_mode" --out out/selection.json
    "$PY" scripts/gen_bundle_project.py --selection out/selection.json --source out/source.json \
        --group-id "$g" --artifact-id "$a" --version "$v" --release-url "$release_url" --snapshot-url "$snapshot_url" \
        --server-id nexus --sources-jar "$sources" --out work/bundle --manifest-out out/bundle-manifest.json
    if [ "$skip_maven" = false ]; then
        mvn -B -ntp -s work/settings.xml -f work/bundle/pom.xml clean verify
        "$PY" scripts/verify_bundle.py --manifest out/bundle-manifest.json --out out/bundle-verification.json
    else
        echo "skipping Maven build and bundle verification (--skip-maven)"
    fi
    tree_arg=()
    [ -n "$tree" ] && tree_arg=(--tree "$tree")
    "$PY" scripts/gen_consumer_patch.py --manifest out/bundle-manifest.json "${tree_arg[@]}" \
        --out out/consumer-patch.md --out-json out/consumer-patch.json
    if [ "$publish" = true ] && [ "$skip_maven" = false ]; then
        mvn -B -ntp -s work/settings.xml -f work/bundle/pom.xml deploy
    fi
    "$PY" scripts/write_summary.py --out-dir out --published "$publish"
    echo "done: see out/SUMMARY.md"

elif [ "$mode" = "verify" ]; then
    old="" new="" bundle_artifact_id="" image_tool=auto platform=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --old) old=$2; shift 2;;
            --new) new=$2; shift 2;;
            --bundle-artifact-id) bundle_artifact_id=$2; shift 2;;
            --image-tool) image_tool=$2; shift 2;;
            --platform) platform=$2; shift 2;;
            *) echo "unknown option: $1"; usage;;
        esac
    done
    [ -n "$old" ] && [ -n "$new" ] || usage
    rm -rf work out && mkdir -p work out
    fetch() {  # <label> <spec>  -> metadata out/<label>-source.json
        local label=$1 spec=$2 type ref p
        if [[ "$spec" == image:* ]]; then
            type=image; ref=${spec#image:}; p=/app/app.jar
            if [[ "$ref" == *"#"* ]]; then p=${ref#*#}; ref=${ref%%#*}; fi
            "$PY" scripts/extract_source.py --type image --ref "$ref" --path "$p" --image-tool "$image_tool" \
                --platform "$platform" --out "work/$label" --meta "out/$label-source.json"
        elif [ -d "$spec" ]; then
            "$PY" scripts/extract_source.py --type dir --ref "$spec" --out "work/$label" --meta "out/$label-source.json"
        else
            "$PY" scripts/extract_source.py --type jar --ref "$spec" --out "work/$label" --meta "out/$label-source.json"
        fi
    }
    fetch old "$old"
    fetch new "$new"
    "$PY" scripts/verify_app_equivalence.py --old-source out/old-source.json --new-source out/new-source.json \
        --bundle-artifact-id "$bundle_artifact_id" --policy config/policy.json --out out/equivalence.json
    "$PY" scripts/write_summary.py --out-dir out
    echo "done: see out/SUMMARY.md"
else
    usage
fi
