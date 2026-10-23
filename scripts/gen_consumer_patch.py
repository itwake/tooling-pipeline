#!/usr/bin/env python3
"""Stage 7: proposal for the consumer project's POM change (never applied automatically).

With one or more `mvn dependency:tree -Dverbose -DoutputType=text` outputs of the consumer
project the proposal is exact: which direct dependencies to delete and which direct
dependencies need an <exclusion> because they transitively bring an embedded coordinate.
Without a tree it explains how to produce one and gives the generic snippets.

The Enforcer rule in the proposal is the real gate: the consumer build fails whenever an
embedded coordinate sneaks back through any path, so incomplete exclusions cannot go
unnoticed.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import ga_str, gav_str, log, parse_dependency_trees, read_json, write_json  # noqa: E402

ENFORCER = """<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-enforcer-plugin</artifactId>
  <version>3.5.0</version>
  <executions>
    <execution>
      <id>ban-bundled-coordinates</id>
      <goals>
        <goal>enforce</goal>
      </goals>
      <configuration>
        <rules>
          <bannedDependencies>
            <excludes>
%s
            </excludes>
            <message>These coordinates are embedded in %s. Add an exclusion on the dependency that reintroduces them (mvn dependency:tree -Dverbose -Dincludes=%s).</message>
          </bannedDependencies>
        </rules>
      </configuration>
    </execution>
  </executions>
</plugin>"""


def local_tag(el) -> str:
    return el.tag.split("}", 1)[-1]


def reduced_pom_dependencies(path: Path):
    deps = []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return deps
    for el in root:
        if local_tag(el) == "dependencies":
            for dep in el:
                if local_tag(dep) == "dependency":
                    deps.append({local_tag(c): (c.text or "").strip() for c in dep})
    return deps


def analyse_trees(texts, embedded_ga):
    modules = []
    for text in texts:
        for tree in parse_dependency_trees(text):
            root = tree["root"]
            module = {"module": ga_str(root), "remove_direct": OrderedDict(), "exclusions": OrderedDict(), "paths": []}
            for node in tree["nodes"]:
                ga = ga_str(node)
                if ga not in embedded_ga:
                    continue
                path = node["path"]
                module["paths"].append(" -> ".join(path[1:]) + (" (%s)" % node["note"] if node.get("note") else ""))
                if node["depth"] == 1:
                    module["remove_direct"].setdefault(ga, {"scope": node.get("scope"), "version": node.get("version")})
                else:
                    direct = path[1]
                    entry = module["exclusions"].setdefault(direct, {"version": None, "targets": []})
                    if ga not in entry["targets"]:
                        entry["targets"].append(ga)
            # version of the direct dependencies that need exclusions
            for node in tree["nodes"]:
                if node["depth"] == 1 and ga_str(node) in module["exclusions"]:
                    module["exclusions"][ga_str(node)]["version"] = node.get("version")
            if module["remove_direct"] or module["exclusions"] or module["paths"]:
                modules.append(module)
            else:
                modules.append(module)  # keep: shows modules untouched
    return modules


def dependency_snippet(bundle: dict) -> str:
    return "\n".join([
        "<dependency>",
        "  <groupId>%s</groupId>" % bundle["groupId"],
        "  <artifactId>%s</artifactId>" % bundle["artifactId"],
        "  <version>%s</version>" % bundle["version"],
        "</dependency>",
    ])


def exclusion_snippet(direct_ga: str, targets, version) -> str:
    g, a = direct_ga.split(":", 1)
    lines = ["<dependency>", "  <groupId>%s</groupId>" % g, "  <artifactId>%s</artifactId>" % a]
    if version:
        lines.append("  <!-- keep your existing version / managed version -->")
    lines.append("  <exclusions>")
    for t in targets:
        tg, ta = t.split(":", 1)
        lines += ["    <exclusion>", "      <groupId>%s</groupId>" % tg, "      <artifactId>%s</artifactId>" % ta, "    </exclusion>"]
    lines += ["  </exclusions>", "</dependency>"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="out/bundle-manifest.json")
    ap.add_argument("--tree", action="append", default=[], help="dependency:tree text output (repeatable)")
    ap.add_argument("--reduced-pom", help="dependency-reduced POM of the bundle (default from manifest)")
    ap.add_argument("--out", default="out/consumer-patch.md")
    ap.add_argument("--out-json", default="out/consumer-patch.json")
    args = ap.parse_args()

    man = read_json(args.manifest)
    bundle = man["bundle"]
    coord = man["coordinate"]
    embedded = [c["gav"] for c in man["components"]]
    embedded_ga = {ga_str(g) for g in embedded}
    reduced_pom = Path(args.reduced_pom or man.get("reduced_pom", ""))
    promoted = [d for d in reduced_pom_dependencies(reduced_pom) if "%s:%s" % (d.get("groupId"), d.get("artifactId")) not in embedded_ga] if reduced_pom.is_file() else []

    texts = []
    for t in args.tree:
        p = Path(t)
        if p.is_file():
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
        else:
            log("WARN: tree file not found: %s" % t)
    modules = analyse_trees(texts, embedded_ga) if texts else []

    includes_arg = ",".join(sorted(embedded_ga))
    md = []
    md.append("# 消费方 POM 修改建议：%s" % coord)
    md.append("")
    md.append("bundle 内嵌的坐标：%s" % ", ".join(gav_str(g) for g in embedded))
    md.append("")
    md.append("Java 代码不需要修改，包名保持不变。需要做的只有三件事：删除被替换的直接依赖，加入 bundle 依赖，在仍会传递引入原坐标的每一条路径上加 exclusion。下面的 Enforcer 规则会在遗漏任何一条路径时让构建失败。")
    md.append("")
    md.append("## 1. 加入 bundle 依赖")
    md.append("")
    md.append("```xml\n%s\n```" % dependency_snippet(bundle))
    md.append("")

    if modules:
        md.append("## 2. 按依赖树生成的修改清单")
        md.append("")
        for m in modules:
            md.append("### 模块 %s" % m["module"])
            md.append("")
            if not m["paths"]:
                md.append("该模块的依赖树中没有出现被内嵌的坐标，无需修改。")
                md.append("")
                continue
            if m["remove_direct"]:
                md.append("删除这些直接依赖：")
                md.append("")
                for ga, info in m["remove_direct"].items():
                    md.append("- `%s`（版本 %s，scope %s）" % (ga, info.get("version"), info.get("scope") or "compile"))
                md.append("")
            if m["exclusions"]:
                md.append("在这些直接依赖上加 exclusion：")
                md.append("")
                for direct, info in m["exclusions"].items():
                    md.append("```xml\n%s\n```" % exclusion_snippet(direct, info["targets"], info.get("version")))
                    md.append("")
            md.append("<details><summary>依赖树中出现的路径（%d 条）</summary>\n" % len(m["paths"]))
            for p in m["paths"]:
                md.append("- %s" % p)
            md.append("\n</details>")
            md.append("")
    else:
        md.append("## 2. 生成精确的修改清单")
        md.append("")
        md.append("本次没有提供消费方工程的依赖树，无法给出逐路径的 exclusion。请在消费方工程根目录执行（多模块工程会把所有模块追加到同一个文件）：")
        md.append("")
        md.append("```bash\nmvn -B org.apache.maven.plugins:maven-dependency-plugin:3.7.1:tree -Dverbose -DoutputType=text -DoutputFile=\"$PWD/dependency-tree.txt\" -DappendOutput=true\n```")
        md.append("")
        md.append("然后重新生成这份建议：")
        md.append("")
        md.append("```bash\npython3 scripts/gen_consumer_patch.py --manifest out/bundle-manifest.json --tree dependency-tree.txt\n```")
        md.append("")
        md.append("或者手工查看每一条引入原坐标的路径：")
        md.append("")
        md.append("```bash\nmvn dependency:tree -Dverbose -Dincludes=%s\n```" % includes_arg)
        md.append("")

    md.append("## 3. Enforcer 门禁（放到父 POM 的 build/plugins，所有模块继承）")
    md.append("")
    md.append("```xml\n%s\n```" % (ENFORCER % ("\n".join("              <exclude>%s</exclude>" % ga for ga in sorted(embedded_ga)), coord, includes_arg)))
    md.append("")
    md.append("## 4. bundle 发布 POM 仍然声明的传递依赖")
    md.append("")
    if promoted:
        md.append("这些依赖没有内嵌，消费方会继续通过 bundle 传递引入；版本以消费方 dependencyManagement（例如 Spring Boot BOM）为准：")
        md.append("")
        for d in promoted:
            md.append("- `%s:%s:%s`%s" % (d.get("groupId"), d.get("artifactId"), d.get("version"), (" (scope %s)" % d["scope"]) if d.get("scope") else ""))
    else:
        md.append("无（或尚未构建 bundle，dependency-reduced POM 不存在）。")
    md.append("")
    md.append("## 5. 验证")
    md.append("")
    md.append("1. 消费方 `mvn -B clean verify`：Enforcer 通过即说明没有路径再引入原坐标。")
    md.append("2. 用 Jenkinsfile.verify 比对修改前后的 app.jar 或镜像：依赖 classpath 上的每个 class 和资源字节一致，只有被替换的 jar 消失、bundle 出现。")
    md.append("3. 跑原有测试，重建镜像，做冒烟。")
    md.append("")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(md), encoding="utf-8")
    write_json(args.out_json, {
        "bundle": bundle,
        "coordinate": coord,
        "embedded": embedded,
        "modules": [{"module": m["module"], "remove_direct": m["remove_direct"], "exclusions": m["exclusions"], "paths": m["paths"]} for m in modules],
        "promoted_dependencies": promoted,
        "tree_provided": bool(texts),
    })
    log("Consumer patch proposal written to %s" % args.out)
    for m in modules:
        log("  %s: remove %d direct, exclusions on %d direct dependencies, %d paths" % (
            m["module"], len(m["remove_direct"]), len(m["exclusions"]), len(m["paths"])))


if __name__ == "__main__":
    main()
