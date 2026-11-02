#!/usr/bin/env python3
"""
Kubernetes lab-manifest similarity comparison.

Implements the metrics from the evaluation-section draft:
  - TES  : Tree Edit Similarity per matched resource (zss tree edit distance)
  - Key-Jaccard / KV-Jaccard : configuration-surface reuse
  - GS   : Graph Similarity (dependency-graph edit distance)
  - Reuse Ratio + clone-type classification (Roy & Cordy taxonomy)

Input: multi-document YAML files (one per lab), each containing K8s objects
       (ConfigMap, Deployment, Service, ...).
Output: CSV + Markdown results table for the given fork relationships.

python3 compare_labs.py /path/to/labs \
  --fork "Lab01:Lab02,Lab06,Lab07,Lab08" \
  --fork "Lab03:Lab04,Lab05,Lab09,Lab10"


python3 -m venv ~/work/hackinsdn/comparison-similarity-labs
source ~/work/hackinsdn/comparison-similarity-labs/bin/activate.fish
python3 -m pip install networkx==3.6.1 pyaml==26.7.0 zss==1.2.0 numpy==2.5.2 scipy==1.18.0
python3 ~/Dropbox/Doutorado-UFBA/HackInSDN-international-paper/comparison-similarity-labs/compare_labs-v2.py ~/Dropbox/Doutorado-UFBA/HackInSDN-international-paper/comparison-similarity-labs/adr015/ --fork "Lab01:Lab02,Lab06,Lab07,Lab08" --fork "Lab03:Lab04,Lab05,Lab09,Lab10"
"""

import re
import csv
import itertools
import yaml
import networkx as nx
from zss import Node, simple_distance

# ---------------------------------------------------------------------------
# 1. Loading & normalization
# ---------------------------------------------------------------------------

VOLATILE_KEYS = {"resourceVersion", "uid", "creationTimestamp", "generation",
                  "status", "selfLink", "managedFields"}

def load_lab(path):
    with open(path, "r", encoding="utf-8") as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    return docs

def strip_volatile(obj):
    """Recursively drop volatile/instance-specific keys."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj

def resource_name(doc):
    return doc.get("metadata", {}).get("name", "<unnamed>")

def resource_kind(doc):
    return doc.get("kind", "<unknown>")

# ---------------------------------------------------------------------------
# 2. Tree Edit Similarity (TES) — Sec. 2.2
# ---------------------------------------------------------------------------

def to_zss_tree(value, key="root"):
    """Convert a parsed YAML value into a zss.Node tree."""
    if isinstance(value, dict):
        node = Node(f"dict:{key}")
        for k in sorted(value.keys()):
            node.addkid(to_zss_tree(value[k], key=k))
        return node
    if isinstance(value, list):
        node = Node(f"list:{key}")
        for i, item in enumerate(value):
            node.addkid(to_zss_tree(item, key=f"{key}[{i}]"))
        return node
    # scalar leaf — label carries both key context and value
    return Node(f"val:{key}={value}")

def node_count(node):
    return 1 + sum(node_count(c) for c in node.children)

def tes(doc_a, doc_b):
    ta, tb = to_zss_tree(doc_a), to_zss_tree(doc_b)
    dist = simple_distance(ta, tb)
    denom = max(node_count(ta), node_count(tb))
    if denom == 0:
        return 1.0
    return 1 - dist / denom

# ---------------------------------------------------------------------------
# 3. Key / KV Jaccard — Sec. 2.3
# ---------------------------------------------------------------------------

def flatten(obj, prefix=""):
    """Yield (key_path, value) pairs for a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flatten(v, f"{prefix}[{i}]")
    else:
        yield (prefix, obj)

def jaccard_sets(docs_a, docs_b):
    kv_a = set()
    key_a = set()
    for d in docs_a:
        for k, v in flatten(d):
            kv_a.add((k, str(v)))
            key_a.add(k)
    kv_b, key_b = set(), set()
    for d in docs_b:
        for k, v in flatten(d):
            kv_b.add((k, str(v)))
            key_b.add(k)
    def jac(a, b):
        if not a and not b:
            return 1.0
        return len(a & b) / len(a | b) if (a | b) else 1.0
    return jac(key_a, key_b), jac(kv_a, kv_b)

# ---------------------------------------------------------------------------
# 4. Dependency graph + Graph Similarity — Sec. 2.4
# ---------------------------------------------------------------------------

def build_graph(docs):
    g = nx.DiGraph()
    for i, d in enumerate(docs):
        node_id = f"{resource_kind(d)}::{i}"
        g.add_node(node_id, kind=resource_kind(d), name=resource_name(d))

    # reference edges: does resource A's serialized text mention resource B's name?
    serials = {}
    for i, d in enumerate(docs):
        node_id = f"{resource_kind(d)}::{i}"
        serials[node_id] = yaml.dump(d)

    names = {f"{resource_kind(d)}::{i}": resource_name(d) for i, d in enumerate(docs)}
    for a_id, a_text in serials.items():
        for b_id, b_name in names.items():
            if a_id == b_id:
                continue
            if b_name and b_name != "<unnamed>" and b_name in a_text:
                g.add_edge(a_id, b_id, kind="ref")

    # label-based Service -> Deployment edges (selector matches template labels)
    deployments = [ (i, d) for i, d in enumerate(docs) if resource_kind(d) == "Deployment" ]
    services   = [ (i, d) for i, d in enumerate(docs) if resource_kind(d) == "Service" ]
    for si, s in services:
        sel = s.get("spec", {}).get("selector", {}) or {}
        for di, dep in deployments:
            labels = dep.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {}) or {}
            if sel and all(labels.get(k) == v for k, v in sel.items()):
                g.add_edge(f"Service::{si}", f"Deployment::{di}", kind="selects")
    return g

def node_subst_cost(n1, n2):
    return 0 if n1.get("kind") == n2.get("kind") else 1

def graph_similarity(g1, g2):
    ged = nx.graph_edit_distance(
        g1, g2,
        node_subst_cost=node_subst_cost,
        node_del_cost=lambda n: 1,
        node_ins_cost=lambda n: 1,
        edge_subst_cost=lambda e1, e2: 0,
        edge_del_cost=lambda e: 1,
        edge_ins_cost=lambda e: 1,
        timeout=5,
    )
    if ged is None:
        return None
    denom = g1.number_of_nodes() + g1.number_of_edges() + g2.number_of_nodes() + g2.number_of_edges()
    return 1 - ged / denom if denom else 1.0

# ---------------------------------------------------------------------------
# 5. Resource matching, reuse ratio, clone typing — Sec. 2.5 / 2.6
# ---------------------------------------------------------------------------

def match_resources(docs_a, docs_b):
    """Greedy best-TES matching within each resource kind."""
    by_kind_a = {}
    for i, d in enumerate(docs_a):
        by_kind_a.setdefault(resource_kind(d), []).append((i, d))
    by_kind_b = {}
    for i, d in enumerate(docs_b):
        by_kind_b.setdefault(resource_kind(d), []).append((i, d))

    matches = []       # (doc_a, doc_b, tes)
    unmatched_b_kinds = set(by_kind_b.keys())

    for kind, a_list in by_kind_a.items():
        b_list = by_kind_b.get(kind, [])
        if not b_list:
            continue
        unmatched_b_kinds.discard(kind) if len(b_list) else None
        pairs = []
        for (ai, ad) in a_list:
            for (bi, bd) in b_list:
                pairs.append((tes(ad, bd), ai, bi))
        pairs.sort(reverse=True)
        used_a, used_b = set(), set()
        for score, ai, bi in pairs:
            if ai in used_a or bi in used_b:
                continue
            used_a.add(ai); used_b.add(bi)
            matches.append((ai, bi, score))

    matched_a_idx = {m[0] for m in matches}
    matched_b_idx = {m[1] for m in matches}
    added   = [i for i in range(len(docs_b)) if i not in matched_b_idx]   # in fork, not in base
    removed = [i for i in range(len(docs_a)) if i not in matched_a_idx]   # in base, not in fork
    return matches, added, removed

def classify(mean_tes, kv_jac, key_jac, gs, theta=0.7):
    if round(mean_tes, 2) >= 0.99 and round(kv_jac, 2) >= 0.99:
        return "Type-1 (exact)"
    if round(mean_tes, 2) >= 0.90 and round(key_jac, 2) >= 0.90 and round(kv_jac, 2) < 0.99:
        return "Type-2 (renamed/parameterized)"
    if gs is not None and round(gs, 2) >= theta:
        return "Type-3 (near-miss)"
    return "Type-4 (semantic clone / divergent)"

# ---------------------------------------------------------------------------
# 6. Driver
# ---------------------------------------------------------------------------

def run(lab_dir, forks, ext=".txt"):
    cache = {}
    def get(lab):
        if lab not in cache:
            docs = load_lab(f"{lab_dir}/{lab}{ext}")
            cache[lab] = [strip_volatile(d) for d in docs]
        return cache[lab]

    rows = []
    for base, fork in forks:
        db, df = get(base), get(fork)
        matches, added, removed = match_resources(db, df)
        mean_tes = sum(m[2] for m in matches) / len(matches) if matches else 0.0
        key_jac, kv_jac = jaccard_sets(db, df)

        gb, gf = build_graph(db), build_graph(df)
        gs = graph_similarity(gb, gf)

        unchanged = sum(1 for m in matches if m[2] >= 0.999)
        modified = len(matches) - unchanged
        #reuse_ratio = (unchanged + modified) / len(df) if df else 0.0
        reuse_ratio = sum(m[2] for m in matches) / len(df) if df else 0.0
        clone_type = classify(mean_tes, kv_jac, key_jac, gs)

        rows.append({
            "Fork": fork, "Base": base,
            "#Resources(fork)": len(df),
            "TES_mean": round(mean_tes, 2),
            "Key_Jaccard": round(key_jac, 2),
            "KV_Jaccard": round(kv_jac, 2),
            "Graph_Similarity": round(gs, 2) if gs is not None else "n/a",
            "Unchanged": unchanged, "Modified": modified,
            "Added": len(added), "Removed": len(removed),
            "ReuseRatio": round(reuse_ratio, 2),
            "CloneType": clone_type,
        })
    return rows

def write_outputs(rows, out_csv, out_md):
    fields = list(rows[0].keys())
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    with open(out_md, "w") as f:
        f.write("| " + " | ".join(fields) + " |\n")
        f.write("|" + "|".join(["---"] * len(fields)) + "|\n")
        for r in rows:
            f.write("| " + " | ".join(str(r[k]) for k in fields) + " |\n")

def parse_fork_args(fork_args):
    """
    Parse repeated --fork "Base:Fork1,Fork2,Fork3" arguments into a flat list
    of (base, fork) pairs.
    """
    pairs = []
    for spec in fork_args:
        if ":" not in spec:
            raise ValueError(f"Invalid --fork spec (expected Base:Fork1,Fork2,...): {spec!r}")
        base, forks_str = spec.split(":", 1)
        base = base.strip()
        for fork in forks_str.split(","):
            fork = fork.strip()
            if fork:
                pairs.append((base, fork))
    return pairs

def parse_fork_file(path):
    """
    Parse a text file with one base lab per line, e.g.:
        Lab01: Lab02, Lab06, Lab07, Lab08
        Lab03: Lab04, Lab05, Lab09, Lab10
    Blank lines and lines starting with '#' are ignored.
    """
    pairs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pairs.extend(parse_fork_args([line]))
    return pairs

def build_arg_parser():
    import argparse
    p = argparse.ArgumentParser(
        description="Compare Kubernetes lab manifests for structural/semantic reuse (TES, "
                     "Jaccard, graph similarity, reuse ratio, clone-type classification)."
    )
    p.add_argument("lab_dir", help="Directory containing the lab manifest files.")
    p.add_argument(
        "--fork", action="append", default=[], metavar="BASE:FORK1,FORK2,...",
        help='Fork relationship, e.g. --fork "Lab01:Lab02,Lab06,Lab07,Lab08". '
             "Repeatable — pass one --fork per base lab.",
    )
    p.add_argument(
        "--forks-file", metavar="PATH",
        help="Path to a text file with one 'Base: Fork1, Fork2, ...' relationship per line "
             "(alternative to repeating --fork).",
    )
    p.add_argument(
        "--ext", default=".txt",
        help="File extension appended to lab names when reading from lab_dir (default: .txt).",
    )
    p.add_argument("--out-csv", default="results.csv", help="Output CSV path (default: results.csv).")
    p.add_argument("--out-md", default="results.md", help="Output Markdown table path (default: results.md).")
    return p

if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    forks = list(parse_fork_args(args.fork))
    if args.forks_file:
        forks.extend(parse_fork_file(args.forks_file))

    if not forks:
        parser.error(
            "No fork relationships given. Use --fork \"Base:Fork1,Fork2\" "
            "(repeatable) and/or --forks-file PATH."
        )

    rows = run(args.lab_dir, forks, ext=args.ext)
    write_outputs(rows, args.out_csv, args.out_md)
    for r in rows:
        print(r)
