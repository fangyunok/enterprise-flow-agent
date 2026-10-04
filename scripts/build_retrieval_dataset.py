"""Build the reproducible retrieval dataset: a scaled policy corpus and a labelled query set.

The corpus keeps the shape of ``demo_seed.json`` policies so the same scoped SQL path serves it, and
it is generated from fixed templates with no randomness, so every run produces byte-identical files.

Usage::

    python scripts/build_retrieval_dataset.py            # write data/policy_corpus.json and queries
    python scripts/build_retrieval_dataset.py --check    # verify committed files match the generator
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "data" / "policy_corpus.json"
QUERY_PATH = ROOT / "data" / "retrieval_eval_queries.jsonl"

TENANTS = [
    {"tenant_id": "alpha", "label": "阿尔法", "cities": ["北京", "上海", "广州", "深圳", "杭州", "成都"],
     "departments": ["engineering", "sales", "finance", "hr"],
     "versions": [("2026-01", "2026-01-01", "2026-10-01"), ("2026-10", "2026-10-01", None)]},
    {"tenant_id": "beta", "label": "贝塔", "cities": ["北京", "上海", "武汉", "西安"],
     "departments": ["engineering", "sales"],
     "versions": [("2026-01", "2026-01-01", None)]},
    {"tenant_id": "gamma", "label": "伽马", "cities": ["南京", "苏州", "重庆"],
     "departments": ["operations"],
     "versions": [("2026-07", "2026-07-01", None)]},
]

DEPARTMENT_LABELS = {"engineering": "研发部", "sales": "销售部", "finance": "财务部", "hr": "人力资源部",
                     "operations": "运营部", "*": "全公司"}

CITY_TIER = {"北京": 1, "上海": 1, "深圳": 1, "广州": 2, "杭州": 2, "南京": 2, "苏州": 2, "武汉": 2,
             "成都": 3, "西安": 3, "重庆": 3}
CITY_CODE = {"北京": "bj", "上海": "sh", "广州": "gz", "深圳": "sz", "杭州": "hz", "成都": "cd",
             "武汉": "wh", "西安": "xa", "南京": "nj", "苏州": "su", "重庆": "cq"}

KINDS = ["hotel", "train", "meal", "taxi"]

BASE_CAPS = {"hotel": {1: 600, 2: 450, 3: 350}, "train": {1: 900, 2: 700, 3: 500},
             "meal": {1: 150, 2: 120, 3: 100}, "taxi": {1: 120, 2: 90, 3: 60}}

VERSION_LABEL = {"2026-01": "2026 年 1 月版", "2026-07": "2026 年 7 月版", "2026-10": "2026 年 10 月版"}

DEPARTMENT_ADJUST = {"engineering": 1.1, "sales": 1.2, "finance": 1.0, "hr": 1.0, "operations": 1.05, "*": 1.0}

TEMPLATES = {
    "hotel": (
        "{city}{scope_label}住宿费报销标准（{version_label}）：员工出差期间每晚住宿开销上限为 {cap} 元，含增值税；"
        "同一城市连续住宿超过 5 晚须经部门负责人书面批准；发票抬头必须为{tenant_label}主体，"
        "超标部分由员工自行承担，靠近会场且报价低于上限时优先选择协议酒店。"
    ),
    "train": (
        "{city}{scope_label}铁路交通报销口径（{version_label}）：高铁二等座、动车二等座与普速硬卧据实报销，"
        "单程车票金额上限 {cap} 元；改签与退票产生的手续费凭凭证报销；当日往返不占用住宿额度；"
        "跨城联程需要说明出差事由与行程必要性。"
    ),
    "meal": (
        "{city}{scope_label}餐费补贴标准（{version_label}）：出差期间每人每日餐饮包干 {cap} 元，涵盖早餐、午餐与晚餐；"
        "陪同客户用餐须单独提交招待申请；超包干部分不予补报；同一餐次不得重复报销。"
    ),
    "taxi": (
        "{city}{scope_label}市内交通报销规则（{version_label}）：出差期间市内出租车与网约车费用凭电子行程单报销，"
        "每日累计上限 {cap} 元；机场大巴、地铁与公交据实报销，不受上限限制；"
        "自驾产生的燃油与过路费不在本条款范围内。"
    ),
}

HANDWRITTEN_QUERIES = [
    ("我下周去上海出差，酒店一晚最多能报多少？", "hotel", "上海", "sales", "2026-10-09"),
    ("北京住店开销的上限标准是什么", "hotel", "北京", "engineering", "2026-10-09"),
    ("深圳高级酒店太贵了，公司给的封顶金额是多少", "hotel", "深圳", "engineering", "2026-10-12"),
    ("广州出差住宿一晚预算上限", "hotel", "广州", "sales", "2026-10-15"),
    ("杭州驻场一周，房费有没有每日封顶", "hotel", "杭州", "engineering", "2026-10-20"),
    ("成都出差开房能报多少", "hotel", "成都", "hr", "2026-10-06"),
    ("高铁票怎么报，二等座有额度吗", "train", "北京", "engineering", "2026-10-08"),
    ("动车票报销上限多少，选二等座行不行", "train", "上海", "sales", "2026-10-11"),
    ("改签手续费这类车票附加费用能不能报", "train", "广州", "finance", "2026-10-14"),
    ("一天之内来回算不算住宿额度", "train", "武汉", "engineering", "2026-10-19"),
    ("吃饭的补助一天给多少", "meal", "北京", "sales", "2026-10-07"),
    ("餐饮包干标准是按天算还是按顿算", "meal", "深圳", "sales", "2026-10-13"),
    ("请客户吃饭的钱走哪条制度", "meal", "上海", "finance", "2026-10-16"),
    ("出差餐补每天多少钱，早中晚都包含吗", "meal", "南京", "operations", "2026-10-18"),
    ("打车费一天能报多少", "taxi", "北京", "engineering", "2026-10-10"),
    ("网约车行程单怎么报，有没有每日额度", "taxi", "上海", "hr", "2026-10-17"),
    ("地铁和机场大巴要不要受额度限制", "taxi", "苏州", "operations", "2026-10-21"),
    ("自己开车去出差，油费和过路费能报吗", "taxi", "重庆", "operations", "2026-10-22"),
]

PARAPHRASE_PATTERNS = [
    ("{city}那边住酒店，一晚的花销封顶是多少钱", "hotel"),
    ("{city}出差的房费上限怎么规定的", "hotel"),
    ("在{city}住宿，报销天花板是哪条", "hotel"),
    ("{city}的住宿开销标准是多少", "hotel"),
    ("{city}坐高铁出差，车票能报的最高金额", "train"),
    ("{city}的铁路票价报销口径", "train"),
    ("去{city}的火车票报销额度是多少", "train"),
    ("{city}出差车票改签退票的钱怎么处理", "train"),
    ("{city}出差吃饭每天补多少钱", "meal"),
    ("{city}期间伙食补贴标准", "meal"),
    ("在{city}的餐补封顶金额", "meal"),
    ("{city}出差饮食开销每天能报多少", "meal"),
    ("{city}市内打车的每日上限", "taxi"),
    ("{city}出差用车费用报销规则", "taxi"),
    ("{city}的出行打车开销有额度吗", "taxi"),
    ("{city}坐地铁公交要不要限额", "taxi"),
]


def build_corpus() -> list[dict]:
    rows = []
    for tenant in TENANTS:
        for kind in KINDS:
            for city in tenant["cities"]:
                tiers = {version[0]: BASE_CAPS[kind][CITY_TIER[city]] for version in tenant["versions"]}
                for version, effective_from, effective_to in tenant["versions"]:
                    rows.append(_clause(tenant, kind, city, version, effective_from, effective_to, "*",
                                        tiers[version], CITY_TIER[city]))
                if version := tenant["versions"][-1][0]:
                    for department in tenant["departments"][:2]:
                        base = BASE_CAPS[kind][CITY_TIER[city]]
                        adjusted = int(round(base * DEPARTMENT_ADJUST[department] / 10.0) * 10)
                        rows.append(_clause(tenant, kind, city, version, tenant["versions"][-1][1],
                                            tenant["versions"][-1][2], department, adjusted, CITY_TIER[city]))
    return sorted(rows, key=lambda row: row["policy_id"])


def _clause(tenant, kind, city, version, effective_from, effective_to, department, cap_cents, tier) -> dict:
    scope_label = "" if department == "*" else DEPARTMENT_LABELS[department]
    identifier = f"{tenant['tenant_id']}-{kind}-{CITY_CODE[city]}-{department.replace('*', 'all')}-{version}"
    return {
        "policy_id": identifier,
        "tenant_id": tenant["tenant_id"],
        "department_scope": department,
        "kind": kind,
        "city": city,
        "version": version,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "clause_id": f"{identifier.upper()}-C1",
        "title": f"{city}{scope_label}{KIND_LABEL[kind]}标准 {version}",
        "content": TEMPLATES[kind].format(city=city, scope_label=scope_label, version_label=VERSION_LABEL[version],
                                          cap=cap_cents, tenant_label=tenant["label"]),
        "cap_cents": cap_cents * 100,
    }


KIND_LABEL = {"hotel": "住宿", "train": "铁路交通", "meal": "餐费补贴", "taxi": "市内交通"}


def build_queries(corpus: list[dict]) -> list[dict]:
    city_tenant = {city: tenant["tenant_id"] for tenant in TENANTS for city in tenant["cities"]}
    tenant_departments = {tenant["tenant_id"]: tenant["departments"] for tenant in TENANTS}

    def expected(tenant_id, kind, city, department, day):
        candidates = [row for row in corpus if row["tenant_id"] == tenant_id and row["kind"] == kind and row["city"] == city
                      and row["department_scope"] in {department, "*"} and row["effective_from"] <= day
                      and (row["effective_to"] is None or row["effective_to"] > day)]
        if not candidates:
            return []
        best = max(candidates, key=lambda row: (row["department_scope"] == department, row["version"]))
        return [best["policy_id"]]

    queries = []
    for order, (text, kind, city, department, day) in enumerate(HANDWRITTEN_QUERIES):
        tenant_id = city_tenant[city]
        if department not in tenant_departments[tenant_id]:
            department = tenant_departments[tenant_id][0]
        queries.append({"query_id": f"q-{order + 1:03d}", "origin": "human", "query": text, "tenant_id": tenant_id,
                        "department_id": department, "trip_date": day,
                        "expected_policy_ids": expected(tenant_id, kind, city, department, day)})
    serial = 0
    for tenant in TENANTS:
        for kind in KINDS:
            for city in tenant["cities"]:
                for pattern, pattern_kind in PARAPHRASE_PATTERNS:
                    if pattern_kind != kind:
                        continue
                    day = tenant["versions"][-1][1]
                    department = tenant["departments"][0]
                    serial += 1
                    queries.append({"query_id": f"g-{serial:03d}", "origin": "generated",
                                    "query": pattern.format(city=city), "tenant_id": tenant["tenant_id"],
                                    "department_id": department, "trip_date": day,
                                    "expected_policy_ids": expected(tenant["tenant_id"], kind, city, department, day)})
    return [query for query in queries if query["expected_policy_ids"]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the retrieval corpus and labelled queries")
    parser.add_argument("--check", action="store_true", help="verify committed files match the generator output")
    arguments = parser.parse_args()

    corpus = build_corpus()
    queries = build_queries(corpus)
    corpus_payload = {"dataset_version": "corpus-1", "notice": "Retrieval corpus for scoped hybrid search.", "policies": corpus}
    corpus_text = json.dumps(corpus_payload, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    query_text = "".join(json.dumps(query, ensure_ascii=False) + "\n" for query in queries)

    if arguments.check:
        drift = []
        if not CORPUS_PATH.exists() or CORPUS_PATH.read_text(encoding="utf-8") != corpus_text:
            drift.append(CORPUS_PATH.name)
        if not QUERY_PATH.exists() or QUERY_PATH.read_text(encoding="utf-8") != query_text:
            drift.append(QUERY_PATH.name)
        if drift:
            print("dataset drift:", ", ".join(drift))
            return 1
        print(f"dataset matches generator: {len(corpus)} clauses, {len(queries)} queries")
        return 0

    CORPUS_PATH.write_text(corpus_text, encoding="utf-8")
    QUERY_PATH.write_text(query_text, encoding="utf-8")
    print(f"wrote {CORPUS_PATH.relative_to(ROOT)}: {len(corpus)} clauses")
    print(f"wrote {QUERY_PATH.relative_to(ROOT)}: {len(queries)} queries "
          f"({sum(1 for query in queries if query['origin'] == 'human')} human)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
