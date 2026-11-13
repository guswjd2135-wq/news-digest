#!/usr/bin/env python3
"""DART 정기보고서 PDF → 재무 비율 요약 다이제스트 (누적 방식) + 정적 페이지.

사용법:
  python3 finance_digest.py <보고서.pdf> [<보고서2.pdf> ...] [--date YYYY-MM-DD]
  python3 finance_digest.py --build-only        # PDF는 건드리지 않고 md/페이지만 다시 생성

동작:
  1) 각 PDF에서 'III. 재무에 관한 사항 > 1. 요약재무정보' 표만 읽는다
     (요약연결/별도 재무상태표·손익계산서). 주석·본문 페이지는 사용하지 않는다.
  2) 계정과목별 숫자를 뽑아 부채비율·ROA·ROE·CIR 등을 계산하고, 보고서당 최대 5건의 요약을 만든다.
  3) 실행할 때마다 결과를 digest/finance/data/YYYY-MM-DD.json 에 덧붙인다(누적).
     같은 PDF(내용 해시 기준)는 어느 날짜에든 이미 있으면 다시 넣지 않는다.
  4) 원본 PDF를 digest/finance/files/ 에 읽기 쉬운 이름으로 복사해 둔다.
  5) 누적 데이터로 digest/finance/YYYY-MM-DD.md 와 digest/finance/index.html 을 다시 만든다.

로컬 서버: python3 finance_digest.py --serve 8766   →  http://127.0.0.1:8766/
필요 패키지: pypdf  (python3 -m pip install --user pypdf)
"""
import argparse
import datetime as dt
import glob
import hashlib
import html
import json
import os
import re
import shutil
import sys

from pypdf import PdfReader

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "digest", "finance")
DATA_DIR = os.path.join(OUT_DIR, "data")
FILES_DIR = os.path.join(OUT_DIR, "files")
SOURCE_URL = "https://dart.fss.or.kr"
MAX_ITEMS = 5  # 보고서 하나당 최대 요약 수

NUM = r"(?:\(?-?[\d,]+\)?|-)"
ROW = re.compile(rf"^(?P<label>.*?\S)\s+(?P<a>{NUM})\s+(?P<b>{NUM})\s+(?P<c>{NUM})$")
PREFIX = re.compile(r"^(?:[IVX]+|\d+|[가-하])\.\s*")
PERIOD = re.compile(r"\((\d{4})\.(\d{1,2})\.(\d{1,2})\s*~\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\)")
PAGE_FOOT = re.compile(r"dart\.fss\.or\.kr Page (\d+)")


# ---------- PDF 읽기 ----------
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def read_report(pdf_path):
    """메타 정보(회사명·보고서 종류)와 요약재무정보 구간의 줄, 쪽 번호를 돌려준다."""
    reader = PdfReader(pdf_path)
    head = "\n".join((reader.pages[i].extract_text() or "") for i in range(min(6, len(reader.pages))))
    flat = re.sub(r"\s+", "", head)
    m = re.search(r"회사명:([^\s:]+?)(?:대표이사|본점|$)", flat)
    company = m.group(1) if m else "회사명 미상"
    kind = next((k for k in ("사업보고서", "반기보고서", "분기보고서") if k in flat), "정기보고서")

    lines, pages, started, first_idx = [], [], False, 1
    sound, sound_left, sound_page, npl = [], 0, None, None
    for idx, pg in enumerate(reader.pages, 1):
        text = pg.extract_text() or ""
        # 'II-5 재무건전성 등 기타 참고사항' (자본적정성·유동성·건전성 지표) — 시작 쪽과 다음 쪽만 읽는다
        if sound_page is None and "재무건전성 등 기타 참고사항" in text and "....." not in text:
            sound_left = 2
            m = PAGE_FOOT.search(text)
            sound_page = int(m.group(1)) if m else idx
        if sound_left:
            sound.extend(text.splitlines())
            sound_left -= 1
        if npl is None:
            m = re.search(r"고정이하여신비율\s*([\d.]+)\s*%", text)
            if m:
                npl = float(m.group(1))
        if not started:
            if ("요약연결재무상태표" not in text and "요약재무상태표" not in text) or "....." in text:
                continue  # 목차 페이지 제외
            started, first_idx = True, idx
        m = PAGE_FOOT.search(text)
        if m:
            pages.append(int(m.group(1)))
        lines.extend(text.splitlines())
        if sum("희석주당이익" in ln for ln in lines) >= 2:  # 별도 손익계산서 끝
            break
    risk_src = parse_soundness(sound)
    if npl is not None:
        risk_src["고정이하여신비율"] = (npl, None, None)
    risk_src["_page"] = sound_page
    return {"company": company, "kind": kind, "pdf_page": first_idx, "soundness": risk_src}, lines, pages


RATIO = r"[\d,]+(?:\.\d+)?(?:\([\d.,]+\))?"
RATIO_ROW = re.compile(rf"^(?P<label>.*?\S)\s+(?P<a>{RATIO})\s+(?P<b>{RATIO})\s+(?P<c>{RATIO})$")
SOUND_KEYS = {  # 보고서 표기 → 저장 이름 (은행 본체 표가 자회사 표보다 먼저 나오므로 첫 행만 쓴다)
    "BIS자기자본비율": "BIS총자본비율", "보통주자본비율": "보통주자본비율", "기본자본비율": "기본자본비율",
    "유동성커버리지비율(LCR)": "LCR", "외화유동성커버리지비율": "외화LCR",
    "대손충당금적립률": "대손충당금적립률", "고정이하여신(B)": "고정이하여신(억원)",
}


def parse_soundness(lines):
    out = {}
    for ln in lines:
        m = RATIO_ROW.match(ln.strip())
        if not m:
            continue
        key = re.sub(r"\s+", "", m.group("label"))
        for k, name in SOUND_KEYS.items():
            if key.startswith(k) and name not in out:
                out[name] = tuple(float(re.sub(r"\(.*\)", "", m.group(x)).replace(",", "")) for x in "abc")
    return out


def to_num(tok):
    tok = tok.strip()
    if tok == "-":
        return 0.0
    neg = tok.startswith("(") and tok.endswith(")")
    v = float(tok.strip("()").replace(",", ""))
    return -v if neg else v


def norm(label):
    return re.sub(r"\s+", "", PREFIX.sub("", label.strip()))


def split_blocks(lines):
    """[연결BS, 연결IS, 별도BS, 별도IS] 블록. 소제목 위치가 섞여 나오므로 각 표의 마지막 행을 경계로 쓴다."""
    blocks, cur, want = [], {}, 0
    ends = ("부채와자본총계", "희석주당이익")
    for ln in lines:
        m = ROW.match(ln.strip())
        if not m:
            continue
        key = norm(m.group("label"))
        cur.setdefault(key, tuple(to_num(m.group(x)) for x in "abc"))
        if key.startswith(ends[want % 2]):
            blocks.append(cur)
            cur, want = {}, want + 1
    return blocks


def period_months(lines):
    for ln in lines:
        m = PERIOD.search(ln)
        if m:
            y1, m1, _, y2, m2, _ = map(int, m.groups())
            return (y2 - y1) * 12 + m2 - m1 + 1, f"{y2}.{m2:02d}"
    return 12, ""


# ---------- 비율 ----------
def get(block, *names, contains=None):
    for n in names:
        for k, v in block.items():
            if k.startswith(n) and (contains is None or contains in k):
                return v
    raise KeyError(names)


def chg(new, old):
    return (new - old) / abs(old) if old else 0.0


def analyze(bs, is_, months):
    ann = 12 / months  # 반기 실적이면 x2 로 연환산
    A, L, E = get(bs, "자산총계"), get(bs, "부채총계"), get(bs, "자본총계")
    loans, dep = get(bs, "상각후원가대출채권", "대출채권"), get(bs, "예수부채")
    ni = get(is_, "반기연결순이익", "분기연결순이익", "연결당기순이익", "당기연결순이익",
             "반기순이익", "분기순이익", "당기순이익")
    nii, top = get(is_, "순이자손익"), get(is_, "총영업이익")
    sga, prov, op = get(is_, "일반관리비"), get(is_, "금융자산손상차손", "신용손실충당금"), get(is_, "영업이익")
    try:
        ni_owner = get(is_, "지배기업주주지분", contains="순이익")
        eq_owner = get(bs, "지배기업주주지분")
    except KeyError:
        ni_owner, eq_owner = ni, E
    avg = lambda t: (t[0] + t[1]) / 2  # 당기말·전기말 평균
    opt = lambda blk, *n: (get(blk, *n) if any(k.startswith(n[0]) for k in blk) else (0.0, 0.0, 0.0))
    bonds, hybrid = opt(bs, "사채"), opt(bs, "신종자본증권")
    oci_debt = opt(is_, "기타포괄손익채무증권평가손익변동")
    return {
        "bond_share": bonds[0] / L[0], "dep_share": dep[0] / L[0], "borrow_share": opt(bs, "차입부채")[0] / L[0],
        "hybrid_share": hybrid[0] / E[0], "oci_debt_ratio": oci_debt[0] / E[0], "oci_debt": oci_debt[0],
        "assets": A[0], "asset_g": chg(A[0], A[1]),
        "debt_ratio": L[0] / E[0], "debt_ratio_prev": L[1] / E[1],
        "equity_ratio": E[0] / A[0],
        "ldr": loans[0] / dep[0], "loan_g": chg(loans[0], loans[1]),
        "ni": ni[0], "ni_g": chg(ni[0], ni[1]), "op_g": chg(op[0], op[1]),
        "nii_g": chg(nii[0], nii[1]), "nii_share": nii[0] / top[0],
        "roa": ni[0] * ann / avg(A), "roe": ni_owner[0] * ann / avg(eq_owner),
        "cir": -sga[0] / top[0], "cir_prev": -sga[1] / top[1],
        "credit_cost": -prov[0] * ann / avg(loans), "prov_g": chg(-prov[0], -prov[1]),
    }



# ---------- 재무 구조 위험성 ----------
LEVELS = ("안정", "주의", "위험")


def _trend(t, unit="%p"):
    """(당기, 전기, 전전기) → '전전기 → 전기 → 당기' 문자열."""
    vals = [v for v in (t[2], t[1], t[0]) if v is not None]
    return " → ".join(f"{v:,.2f}" for v in vals)


def assess_risk(c, snd):
    """연결 비율(c)과 재무건전성 지표(snd)로 영역별 위험 수준을 매긴다.
    기준은 국내 은행 규제 최저치(바젤III)와 일반적인 경계선이며, 은행별 추가 적립(D-SIB 등)은 반영하지 않는다."""
    rows = []

    def add(area, name, value, basis, level, note):
        rows.append({"area": area, "name": name, "value": value, "basis": basis,
                     "level": LEVELS[level], "note": note})

    def lvl(v, warn, bad, low_is_bad=True):
        if low_is_bad:
            return 2 if v < bad else (1 if v < warn else 0)
        return 2 if v >= bad else (1 if v >= warn else 0)

    snd = snd or {}
    if "BIS총자본비율" in snd:
        t = snd["BIS총자본비율"]
        add("자본적정성", "BIS 총자본비율", _trend(t) + "%", "규제 10.5% (최저 8% + 보존완충 2.5%)",
            lvl(t[0], 13, 10.5), f"규제선보다 {t[0] - 10.5:.2f}%p 높다.")
    if "보통주자본비율" in snd:
        t = snd["보통주자본비율"]
        add("자본적정성", "보통주자본(CET1)비율", _trend(t) + "%", "규제 7.0% (최저 4.5% + 완충 2.5%)",
            lvl(t[0], 9.5, 7.0), f"손실을 가장 먼저 흡수하는 자본의 여유가 {t[0] - 7.0:.2f}%p다.")
    if c:
        hs = c["hybrid_share"] * 100
        add("자본적정성", "신종자본증권 / 자본총계", f"{hs:.2f}%", "15% 이상이면 자본의 질 주의",
            lvl(hs, 15, 25, low_is_bad=False), "이자를 지급하는 영구채라 보통주 자본보다 손실흡수력이 약하지만, 비중은 아직 기준 이하다.")
        er = c["equity_ratio"] * 100
        add("레버리지", "자기자본비율(자본/자산)", f"{er:.2f}% · 부채비율 {c['debt_ratio'] * 100:,.0f}%",
            "은행은 7% 안팎이 일반적, 5% 미만 위험", lvl(er, 7, 5),
            "자산이 자본의 약 {:.0f}배라 자산가치가 {:.1f}%만 하락해도 자본이 소진되는 구조다.".format(1 / c["equity_ratio"], er))
    if "LCR" in snd:
        t = snd["LCR"]
        down = t[0] < t[1] < t[2]
        add("유동성", "유동성커버리지비율(LCR)", _trend(t) + "%", "규제 100%",
            lvl(t[0], 110, 100), f"규제선 대비 여유가 {t[0] - 100:.2f}%p에 불과" + ("하고 3기 연속 낮아졌다." if down else "하다."))
    if "외화LCR" in snd:
        t = snd["외화LCR"]
        add("유동성", "외화 LCR", _trend(t) + "%", "규제 80%", lvl(t[0], 100, 80),
            "여유는 크지만 " + ("3기 연속 하락 중이다." if t[0] < t[1] < t[2] else "추이를 지켜볼 필요가 있다."))
    if c:
        bs_ = c["bond_share"] * 100
        add("조달구조", "사채 / 부채총계", f"{bs_:.1f}% (예수부채 {c['dep_share'] * 100:.1f}%, 차입부채 {c['borrow_share'] * 100:.1f}%)",
            "40% 이상이면 시장성 조달 의존 주의", lvl(bs_, 40, 55, low_is_bad=False),
            "예금보다 채권 발행 비중이 커서 금리·채권시장 상황에 따라 조달비용과 차환 위험이 커질 수 있다.")
    if "고정이하여신비율" in snd:
        v = snd["고정이하여신비율"][0]
        add("자산건전성", "고정이하여신(NPL)비율", f"{v:.2f}% (본문 기재값)", "1% 이상 주의, 2% 이상 위험",
            lvl(v, 1.0, 2.0, low_is_bad=False), "부실 여신 비중이 1%를 넘어섰다." if v >= 1 else "부실 여신 비중이 낮다.")
    if "대손충당금적립률" in snd:
        t = snd["대손충당금적립률"]
        drop = t[2] - t[0]
        lv = lvl(t[0], 120, 100)
        if drop >= 5 and lv == 0:
            lv = 1
        npl_amt = snd.get("고정이하여신(억원)")
        extra = f" 고정이하여신은 {npl_amt[2]:,.0f}억 → {npl_amt[0]:,.0f}억원으로 늘었다." if npl_amt and npl_amt[0] > npl_amt[2] else ""
        add("자산건전성", "대손충당금적립률(충당금/고정이하여신)", _trend(t) + "%", "100% 미만 위험, 120% 미만 주의",
            lv, f"2기 전보다 {drop:.1f}%p 낮아져 부실 대비 완충력이 얇아지고 있다." + extra if drop > 0 else "완충력이 유지되고 있다." + extra)
    if c:
        cc = c["credit_cost"] * 100
        lv = lvl(cc, 0.5, 0.8, low_is_bad=False)
        if c["prov_g"] > 0.10 and lv == 0:
            lv = 1
        add("자산건전성", "대손비용률(연환산)", f"{cc:.2f}% (손상차손 전년비 {c['prov_g'] * 100:+.1f}%)",
            "0.5% 이상 주의, 또는 손상차손 10% 이상 증가", lv, "충당금 부담이 이익을 깎는 주된 요인이다." if c["prov_g"] > 0 and c["ni_g"] < 0 else "충당금 부담은 관리 범위다.")
        ng = c["ni_g"] * 100
        add("수익성", "순이익 증감(전년 동기)", f"{ng:+.2f}% · ROE {c['roe'] * 100:.2f}%", "감소면 주의, 10% 이상 감소 위험",
            lvl(ng, 0, -10), "이자이익은 늘었지만 대손비용·관리비 증가로 순이익이 줄었다." if ng < 0 and c["nii_g"] > 0 else "이익 흐름이 유지되고 있다.")
        oc = c["oci_debt_ratio"] * 100
        add("금리위험", "채무증권 평가손익 / 자본", f"{oc:+.2f}% ({eok(c['oci_debt'])})", "자본의 -0.5% 이하 손실이면 주의",
            1 if oc <= -0.5 else 0, "채권 가격 하락(금리 상승 등)으로 생긴 평가손이 자본(기타포괄손익)을 줄였다." if oc < 0 else "보유 채권 평가이익이 자본에 보탬이 됐다.")

    watch = [r["name"] for r in rows if r["level"] != "안정"]
    bad = [r["name"] for r in rows if r["level"] == "위험"]
    safe_areas = sorted({r["area"] for r in rows} - {r["area"] for r in rows if r["level"] != "안정"})
    if not rows:
        overall = "해당 없음"
    else:
        watch_areas = list(dict.fromkeys(r["area"] for r in rows if r["level"] != "안정"))
        if safe_areas and watch:
            first = (f"{'·'.join(safe_areas)}은 기준을 여유 있게 넘지만, {'·'.join(watch_areas)} 영역에서 "
                     f"점검이 필요한 지표가 {len(watch)}개 있다.")
        elif watch:
            first = f"모든 영역({'·'.join(watch_areas)})에 점검이 필요한 지표가 있다({len(watch)}개)."
        else:
            first = "모든 지표가 기준을 여유 있게 넘는다."
        second = (f"특히 {', '.join(bad)}은 기준 미달이므로 우선 확인해야 한다." if bad else
                  "당장 규제 미달인 지표는 없지만, 유동성 여유가 얇고 부실 완충력이 줄며 시장성 조달 의존이 높아 "
                  "경기 둔화·금리 변동이 겹치면 부담이 빠르게 커질 수 있는 구조다." if watch else
                  "현재 수치상 재무 구조 위험은 낮다.")
        overall = first + " " + second
    return {"overall": overall, "rows": rows}

# ---------- 요약 문장 ----------
pct = lambda x: f"{x * 100:,.2f}%"
jo = lambda x: f"{x / 1_000_000:,.1f}조원"   # 백만원 → 조원
eok = lambda x: f"{x / 100:,.0f}억원"        # 백만원 → 억원
up = lambda x: "늘었" if x >= 0 else "줄었"


def make_items(c, s, label):
    """label: '반기', '분기', '연간' 등 실적 기간 표현."""
    out = []
    if c:
        out.append(("연결 재무상태표 — 부채비율·자기자본비율",
            f"기말 연결 자산은 {jo(c['assets'])}으로 전기말보다 {pct(abs(c['asset_g']))} {up(c['asset_g'])}고, "
            f"부채비율(부채/자본)은 {pct(c['debt_ratio_prev'])}에서 {pct(c['debt_ratio'])}로 바뀌었다. "
            f"자기자본비율(자본/자산)은 {pct(c['equity_ratio'])}이며, 대출채권을 예수부채로 나눈 단순 예대비율은 "
            f"{pct(c['ldr'])}로 예금 외 조달(사채 등) 비중을 가늠하게 해 준다.",
            {"부채비율": c["debt_ratio"], "자기자본비율": c["equity_ratio"], "예대비율(단순)": c["ldr"]}))
        out.append(("연결 손익계산서 — 이익 증감률",
            f"{label} 연결순이익은 {eok(c['ni'])}으로 전년 동기 대비 {pct(abs(c['ni_g']))} {up(c['ni_g'])}고, "
            f"영업이익은 {pct(abs(c['op_g']))} {up(c['op_g'])}다. "
            f"순이자손익은 {pct(abs(c['nii_g']))} {up(c['nii_g'])}으며 총영업이익의 {pct(c['nii_share'])}를 차지한다.",
            {"순이익 증감": c["ni_g"], "순이자손익 증감": c["nii_g"], "이자이익 비중": c["nii_share"]}))
        tail = ("자산 증가에 비해 이익이 줄어 자본 효율이 전년보다 다소 낮아진 모습이다." if c["ni_g"] < 0
                else "이익이 자산 증가를 따라가며 자본 효율이 유지되고 있다.")
        out.append(("연결 수익성 — ROA·ROE(연환산)",
            f"순이익을 연 단위로 환산해 기초·기말 평균잔액으로 나누면 ROA는 {pct(c['roa'])}, "
            f"지배주주 기준 ROE는 {pct(c['roe'])}이다. {tail}",
            {"ROA": c["roa"], "ROE": c["roe"]}))
        tail = (" 순이익 감소에 크게 작용했다." if c["prov_g"] > 0 and c["ni_g"] < 0 else "다.")
        out.append(("연결 효율성·건전성 — CIR·대손비용률",
            f"일반관리비를 총영업이익으로 나눈 CIR은 {pct(c['cir'])}로 전년 동기 {pct(c['cir_prev'])}와 비교된다. "
            f"금융자산 손상차손은 전년보다 {pct(abs(c['prov_g']))} {up(c['prov_g'])}고, 평균 대출채권 대비 "
            f"연환산 대손비용률은 {pct(c['credit_cost'])}로" + (tail if tail != "다." else " 집계된다."),
            {"CIR": c["cir"], "대손비용률": c["credit_cost"]}))
    if s:
        share = f" 연결 순이익의 {pct(s['ni'] / c['ni'])}가 별도(본체)에서 나온다." if c and c["ni"] else ""
        out.append(("별도(본체) 재무제표 — 핵심 비율",
            f"별도 기준 {label} 순이익은 {eok(s['ni'])}으로 전년 동기 대비 {pct(abs(s['ni_g']))} {up(s['ni_g'])}고, "
            f"연환산 ROA {pct(s['roa'])}·ROE {pct(s['roe'])}, CIR {pct(s['cir'])}이다. "
            f"부채비율은 {pct(s['debt_ratio'])}이고 대출채권은 전기말보다 {pct(abs(s['loan_g']))} {up(s['loan_g'])}으며,{share}",
            {"ROA": s["roa"], "ROE": s["roe"], "CIR": s["cir"], "부채비율": s["debt_ratio"]}))
    return [{"title": t, "summary": sm.strip(), "ratios": r} for t, sm, r in out[:MAX_ITEMS]]


# ---------- 누적 저장 ----------
def load_day(day):
    path = os.path.join(DATA_DIR, f"{day}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"date": day, "reports": []}


def save_day(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, f"{data['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def all_days():
    return sorted((os.path.basename(p)[:-5] for p in glob.glob(os.path.join(DATA_DIR, "*.json"))), reverse=True)


def known_ids():
    return {r["id"] for d in all_days() for r in load_day(d)["reports"]}


def process(pdf):
    rid = sha(pdf)
    meta, lines, pages = read_report(pdf)
    months, end = period_months(lines)
    blocks = split_blocks(lines)
    cons = sep = None
    try:
        if len(blocks) >= 2:
            cons = analyze(blocks[0], blocks[1], months)
        if len(blocks) >= 4:
            sep = analyze(blocks[2], blocks[3], months)
    except (KeyError, ZeroDivisionError) as e:
        print(f"  계정과목을 찾지 못함: {e}", file=sys.stderr)
    label = {6: "반기", 3: "1분기", 9: "3분기", 12: "연간"}.get(months, f"{months}개월")
    items = make_items(cons, sep, label)
    risk = assess_risk(cons, meta["soundness"])
    risk["page"] = meta["soundness"].get("_page")
    if not items:
        return None
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", os.path.basename(pdf))
    filed = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
    fname = f"{meta['company']}_{meta['kind']}_{filed or rid}.pdf"
    os.makedirs(FILES_DIR, exist_ok=True)
    shutil.copy2(pdf, os.path.join(FILES_DIR, fname))
    first = min(pages) if pages else 1
    return {
        "id": rid, "company": meta["company"], "kind": meta["kind"], "period_end": end,
        "filed": filed, "file": f"files/{fname}", "pages": [first, max(pages) if pages else first],
        "pdf_page": meta["pdf_page"],  # PDF 뷰어에서 바로 열 쪽
        "added": dt.datetime.now().isoformat(timespec="seconds"), "items": items, "risk": risk,
    }


# ---------- 출력 ----------
def source_line(r):
    return (f"{SOURCE_URL} ({r['company']} {r['kind']} III-1 요약재무정보 p.{r['pages'][0]}~{r['pages'][1]}) · "
            f"PDF: {r['file']}")


def render_md(data):
    parts = ["# 재무 분석 요약", ""]
    if not data["reports"]:
        parts.append("해당 없음")
    for r in data["reports"]:
        parts += [f"## {r['company']} {r['kind']} ({r['period_end']} 기준)", ""]
        for it in r["items"]:
            parts += [f"### {it['title']}", "", it["summary"], "", f"원문: {source_line(r)}", ""]
        rk = r.get("risk")
        if rk and rk["rows"]:
            parts += ["### 재무 구조 위험성 분석", "", rk["overall"], "",
                      "| 영역 | 지표 | 값 (추이) | 기준 | 판단 | 해석 |", "|---|---|---|---|---|---|"]
            parts += [f"| {x['area']} | {x['name']} | {x['value']} | {x['basis']} | {x['level']} | {x['note']} |"
                      for x in rk["rows"]]
            pg = f" · 재무건전성 지표 p.{rk['page']}~{rk['page'] + 1}" if rk.get("page") else ""
            parts += ["", f"원문: {SOURCE_URL} ({r['company']} {r['kind']} 요약재무정보{pg})",
                      "", "> 기준선은 국내 은행 규제 최저치와 일반적 경계선이며, 은행별 추가 자본 요구는 반영하지 않았다.", ""]
    path = os.path.join(OUT_DIR, f"{data['date']}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts).rstrip() + "\n")
    return path


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>재무 분석 요약</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mute:#6b6b70;--line:#e3e3e0;--acc:#1f5fbf;--pos:#1a7f4b;--neg:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--card:#1f1f22;--fg:#ececee;--mute:#9a9aa1;--line:#2e2e33;--acc:#7aa7ff;--pos:#5cc98f;--neg:#ff7b72}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}}
main{{max-width:880px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:24px;margin:0 0 4px}}
.sub{{color:var(--mute);margin:0 0 24px}}h2{{font-size:18px;margin:32px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}}
.card h3{{margin:0 0 6px;font-size:16px}}.card h4{{margin:14px 0 4px;font-size:14px}}.card p{{margin:0 0 6px}}
.meta{{color:var(--mute);font-size:13px}}a{{color:var(--acc)}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}}.chip{{border:1px solid var(--line);border-radius:999px;padding:1px 10px;font-size:13px;font-variant-numeric:tabular-nums}}
.neg{{color:var(--neg)}}.pos{{color:var(--pos)}}
table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}
.wrap{{overflow-x:auto}}
.risk{{margin-top:18px;border-top:1px dashed var(--line);padding-top:10px}}.risk td{{vertical-align:top}}.risk td:first-child{{white-space:nowrap}}
.lv{{display:inline-block;border-radius:6px;padding:0 8px;font-size:12px;font-weight:600;white-space:nowrap}}
.lv-안정{{background:color-mix(in srgb,var(--pos) 18%,transparent);color:var(--pos)}}
.lv-주의{{background:color-mix(in srgb,#d08a00 22%,transparent);color:#b07400}}
.lv-위험{{background:color-mix(in srgb,var(--neg) 18%,transparent);color:var(--neg)}}
.sum{{display:flex;gap:8px;margin:6px 0 10px}}
</style></head><body><main>
<h1>재무 분석 요약</h1>
<p class="sub">DART 정기보고서의 요약재무정보로 계산한 비율 · 마지막 생성 {built}</p>
<h2>받은 보고서 파일</h2>
<div class="wrap"><table><tr><th>파일</th><th>크기</th><th>추가된 날</th></tr>{files}</table></div>
{days}
</main></body></html>
"""


def chip(name, v):
    cls = " neg" if ("증감" in name and v < 0) else (" pos" if "증감" in name else "")
    return f'<span class="chip{cls}">{html.escape(name)} {pct(v)}</span>'


def build_site():
    os.makedirs(OUT_DIR, exist_ok=True)
    added = {}
    sections = []
    for d in all_days():
        data = load_day(d)
        cards = []
        for r in data["reports"]:
            added.setdefault(r["file"], d)
            href = html.escape(f"{r['file']}#page={r['pdf_page']}")
            body = "".join(
                f"<h4>{html.escape(it['title'])}</h4><p>{html.escape(it['summary'])}</p>"
                f"<div class='chips'>{''.join(chip(k, v) for k, v in it['ratios'].items())}</div>"
                for it in r["items"])
            rk = r.get("risk") or {}
            if rk.get("rows"):
                cnt = {lv: sum(x["level"] == lv for x in rk["rows"]) for lv in LEVELS}
                badges = "".join('<span class="lv lv-%s">%s %d</span>' % (k, k, v) for k, v in cnt.items())
                body += ("<div class='risk'><h4>재무 구조 위험성 분석</h4>"
                         f"<div class='sum'>{badges}</div>"
                         f"<p>{html.escape(rk['overall'])}</p><div class='wrap'><table>"
                         "<tr><th>영역</th><th>지표</th><th>값 (추이)</th><th>판단</th><th>해석 · 기준</th></tr>"
                         + "".join(f"<tr><td>{html.escape(x['area'])}</td><td>{html.escape(x['name'])}</td>"
                                   f"<td>{html.escape(x['value'])}</td><td><span class='lv lv-{x['level']}'>{x['level']}</span></td>"
                                   f"<td>{html.escape(x['note'])}<div class='meta'>{html.escape(x['basis'])}</div></td></tr>"
                                   for x in rk["rows"])
                         + "</table></div><p class='meta'>기준선은 국내 은행 규제 최저치와 일반적 경계선이며, 은행별 추가 자본 요구는 반영하지 않았다.</p></div>")
            cards.append(
                f"<div class='card'><h3>{html.escape(r['company'])} {html.escape(r['kind'])}</h3>"
                f"<div class='meta'>{html.escape(r['period_end'])} 기준 · 제출 {html.escape(r['filed'] or '-')} · "
                f"원문 <a href='{SOURCE_URL}' target='_blank' rel='noopener'>DART</a> "
                f"p.{r['pages'][0]}~{r['pages'][1]} · <a href='{href}' target='_blank'>PDF 열기</a></div>{body}</div>")
        sections.append(f"<h2>{d}</h2>" + ("".join(cards) or "<p>해당 없음</p>"))
    rows = []
    for p in sorted(glob.glob(os.path.join(FILES_DIR, "*.pdf"))):
        rel = f"files/{os.path.basename(p)}"
        size = os.path.getsize(p) / 1_048_576
        rows.append(f"<tr><td><a href='{html.escape(rel)}' download>{html.escape(os.path.basename(p))}</a></td>"
                    f"<td>{size:.1f} MB</td><td>{added.get(rel, '-')}</td></tr>")
    page = PAGE.format(built=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                       files="".join(rows) or "<tr><td colspan=3>아직 없음</td></tr>",
                       days="".join(sections) or "<p>해당 없음</p>")
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def refresh(pdf):
    rid = sha(pdf)
    for d in all_days():
        data = load_day(d)
        for i, r in enumerate(data["reports"]):
            if r["id"] == rid:
                new = process(pdf)
                if new:
                    new["added"] = r["added"]
                    data["reports"][i] = new
                    save_day(data)
                    render_md(data)
                return


def serve(port):
    """digest/finance 폴더만 로컬에서 서빙한다 (외부 접속 없음)."""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    build_site()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=OUT_DIR)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"http://127.0.0.1:{port}/ 에서 서빙 중 (Ctrl+C 로 종료)", flush=True)
        httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="*")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="이미 넣은 PDF도 다시 분석해 원래 날짜 기록을 갱신")
    ap.add_argument("--serve", type=int, metavar="PORT", help="페이지를 다시 만든 뒤 127.0.0.1:PORT 로 서빙")
    args = ap.parse_args()
    if args.serve:
        return serve(args.serve)

    added = 0
    if not args.build_only:
        new_reports, seen = [], known_ids()
        for pdf in args.pdfs:
            name = os.path.basename(pdf)
            if sha(pdf) in seen:
                if args.refresh:
                    refresh(pdf)
                    print(f"다시 분석함: {name}")
                else:
                    print(f"건너뜀(이미 있음): {name}")
                continue
            rep = process(pdf)
            if rep is None:
                print(f"건너뜀(요약재무정보 없음): {name}")
                continue
            new_reports.append(rep)
            seen.add(rep["id"])
            added += len(rep["items"])
        data = load_day(args.date)  # refresh 반영 후의 최신 기록에 덧붙인다
        data["reports"].extend(new_reports)
        save_day(data)
        path = render_md(data)
    build_site()
    if not args.build_only:
        print(f"{os.path.relpath(path, BASE_DIR)} 저장, 새로 고른 자료 {added}건 "
              f"(오늘 누적 {sum(len(r['items']) for r in data['reports'])}건)")


if __name__ == "__main__":
    main()
