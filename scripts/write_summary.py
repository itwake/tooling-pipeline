#!/usr/bin/env python3
"""Collate the JSON outputs of a run into out/SUMMARY.md."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import gav_str, log, read_json  # noqa: E402


def load(path: Path):
    return read_json(path) if path.is_file() else None


def checks_table(checks):
    lines = ["| 检查 | 结果 | 说明 |", "|---|---|---|"]
    for c in checks:
        lines.append("| %s | %s | %s |" % (c["check"], c["status"].upper(), (c.get("detail") or "").replace("|", "\\|")[:300]))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--published", default="", help="'true' when the bundle was deployed")
    args = ap.parse_args()
    out = Path(args.out_dir)

    source = load(out / "source.json")
    inventory = load(out / "inventory.json")
    selection = load(out / "selection.json")
    manifest = load(out / "bundle-manifest.json")
    verification = load(out / "bundle-verification.json")
    patch = load(out / "consumer-patch.json")
    equivalence = load(out / "equivalence.json")

    md = ["# 运行摘要", ""]
    if source:
        md += ["## 输入", "", "- 类型：%s" % source["type"], "- 引用：`%s`" % source["ref"]]
        if source.get("app_path"):
            md.append("- 镜像内路径：`%s`" % source["app_path"])
        if source.get("digest"):
            md.append("- 镜像摘要：`%s`" % source["digest"])
        if source.get("sha256"):
            md.append("- 制品 SHA-256：`%s`" % source["sha256"])
        md.append("")
    if inventory:
        app = inventory["app"]
        md += ["## 应用清点", "", "- 布局：%s" % app["layout"], "- Spring Boot：%s" % (app.get("spring_boot_version") or "未检测到"),
               "- 依赖 jar 数量：%d" % app["dependency_count"]]
        unknown = [d["name"] for d in inventory["dependencies"] if not d.get("gav")]
        if unknown:
            md.append("- 无法识别坐标：%s" % ", ".join(unknown))
        md.append("")
    if selection:
        md += ["## 选择与策略（%s 模式）" % selection["policy_mode"], ""]
        for d in selection["selected"]:
            md.append("- `%s` ← %s" % (gav_str(d["gav"]), d["name"]))
        md.append("")
        if selection["findings"]:
            md += ["| 级别 | 代码 | 说明 |", "|---|---|---|"]
            for f in selection["findings"]:
                md.append("| %s | %s | %s |" % (f["level"].upper(), f["code"], f["message"].replace("|", "\\|")))
            md.append("")
    if manifest:
        md += ["## bundle", "", "- 坐标：`%s`" % manifest["coordinate"], "- 内容：`%s`" % manifest["contents_attr"],
               "- 来源：%s" % manifest["source_desc"], "- 发布：%s" % ("已发布到 Nexus" if args.published == "true" else "未发布（仅构建与校验）"), ""]
    if verification:
        md += ["## bundle 校验：%s" % ("通过" if verification["ok"] else "失败"), "", checks_table(verification["checks"]), ""]
        if verification.get("promoted_dependencies"):
            md.append("发布 POM 中保留的传递依赖：" + ", ".join("`%s:%s:%s`" % (d.get("groupId"), d.get("artifactId"), d.get("version")) for d in verification["promoted_dependencies"]))
            md.append("")
    if patch:
        md += ["## 消费方修改建议", "", "见 consumer-patch.md。%s" % ("已按依赖树生成逐模块清单。" if patch.get("tree_provided") else "未提供消费方依赖树，只有通用说明。"), ""]
    if equivalence:
        md += ["## 应用等价性校验：%s" % ("通过" if equivalence["ok"] else "失败"), "",
               "- 原应用：`%s`" % equivalence["old"], "- 新应用：`%s`" % equivalence["new"],
               "- bundle：%s" % equivalence["bundle"]["file"], "", checks_table(equivalence["checks"]), ""]

    (out / "SUMMARY.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    log("Summary written to %s" % (out / "SUMMARY.md"))


if __name__ == "__main__":
    main()
