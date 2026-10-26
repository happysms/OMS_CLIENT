"""Render reviewable Korean documentation diagrams without external dependencies.

Edit docs/figures/*-scenes.json, then run this script. --check detects stale SVG,
Mermaid, case pages, broken illustration links and catalog coverage drift.
Only documentation artifacts are written; runtime code and the case catalog are read-only.
"""
from collections import Counter
from html import escape
import argparse
import json
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
FIGURES = ROOT / 'docs/figures'
GROUPS = {
    'A': '접수·규격·자금·거래소 구성', 'B': '도착 가격·즉시 체결',
    'C': '대기 지정가·외부 대기열', 'D': '복수 가격·복수 주문',
    'E': '취소와 체결 경합', 'F': '정정·교체·재주문',
    'G': '자기체결 방지·매칭 제한', 'H': 'OKX 선물·포지션·담보',
    'I': '두 거래소·헤지 경로', 'J': 'ACK·체결·계좌 통지',
    'K': '자료·깊이·정합 불확실성', 'L': '종료·확장·지원 경계',
}
STATUSES = {
    'implemented': ('실행·차단 연결', '#0f766e', '#ecfdf5'),
    'model_assumption': ('명시적 모형 가정', '#1d4ed8', '#eff6ff'),
    'partial': ('부분 지원 · 설계와 현행 범위 구분', '#92400e', '#fffbeb'),
    'unsupported': ('설계 제안 · 현재 실행 미지원', '#6b21a8', '#faf5ff'),
}


def read_json(path):
    return json.loads(path.read_text())


def wrap(text, width):
    """Conservative display-width wrapping, including Korean and ASCII formulas."""
    result, line, size = [], '', 0
    for char in text:
        char_size = 2 if unicodedata.east_asian_width(char) in 'WF' else 1
        if size + char_size > width and line:
            result.append(line.rstrip())
            line, size = '', 0
        line += char
        size += char_size
    if line:
        result.append(line.rstrip())
    return result


def text_lines(lines, x, y, size, color, leading, weight='400'):
    children = ''.join(f'<tspan x="{x}" y="{y+i*leading}">{escape(line)}</tspan>' for i, line in enumerate(lines))
    return f'<text font-size="{size}" fill="{color}" font-weight="{weight}">{children}</text>'


def render_svg(scene, status_key=None):
    title = scene['title']
    label, color, background = STATUSES.get(status_key, (scene.get('status', '설명 모형'), '#334155', '#f1f5f9'))
    title_lines = wrap(title, 74)
    steps = [[line for paragraph in step for line in wrap(paragraph, 28)] for step in scene['steps']]
    assert len(steps) == 3, scene['id']
    # Fixed-width three-panel storyboard; height grows with actual wrapped content.
    top = 100 + len(title_lines) * 32
    card_height = 78 + max(map(len, steps)) * 28
    footer = top + card_height + 38
    queue = scene.get('queue_example')
    height = footer + 64 + (176 if queue else 0)
    description = ' → '.join(' / '.join(s) for s in scene['steps'])
    pieces = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="{height}" viewBox="0 0 1040 {height}" role="img" aria-labelledby="title description">',
              f'<title id="title">{escape(title)}</title>', f'<desc id="description">{escape(label + "。" + description)}</desc>',
              '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#64748b"/></marker></defs>',
              '<rect width="1040" height="100%" rx="16" fill="#fff"/>',
              '<g font-family="Noto Sans CJK KR, Noto Sans KR, sans-serif">',
              text_lines([f'OMS SIMULATION  /  {scene["id"].upper()}'],32,32,14,'#64748b',20,'600'),
              text_lines(title_lines,32,68,24,'#0f172a',32,'700')]
    badge_y = top - 43
    pieces += [f'<rect x="32" y="{badge_y}" width="976" height="30" rx="7" fill="{background}"/>',
               text_lines([label],46,badge_y+21,15,color,20,'600')]
    dashed = ' stroke-dasharray="6 4"' if status_key == 'unsupported' else ''
    for i, lines in enumerate(steps):
        x = 32 + 332*i
        pieces += [f'<rect x="{x}" y="{top}" width="312" height="{card_height}" rx="12" fill="#f8fafc" stroke="#cbd5e1"{dashed}/>',
                   f'<rect x="{x}" y="{top}" width="312" height="5" rx="2" fill="{color}"/>',
                   text_lines([['01  상황·입력','02  판단·변화','03  결과·보존'][i]],x+20,top+35,16,color,22,'700'),
                   text_lines(lines,x+20,top+72,19,'#1e293b',28)]
        if i < 2:
            middle = top + card_height//2
            pieces.append(f'<path d="M{x+315} {middle} L{x+326} {middle}" fill="none" stroke="#64748b" stroke-width="2" marker-end="url(#arrow)"{dashed}/>')
    if queue:
        pieces.append(text_lines([f'수량 비교  ·  반대 틱 {queue["tick"]}개  ·  막대는 같은 축척'],32,footer,17,'#0f172a',24,'600'))
        denominator = sum(s['quantity'] for s in queue['before'])
        assert denominator > 0
        for row_index, key in enumerate(('before', 'after')):
            y = footer + 18 + row_index * 60
            pieces.append(text_lines(['처리 전' if key == 'before' else '처리 후'],32,y+25,16,'#334155',22,'600'))
            x = 132
            for segment in queue[key]:
                quantity = segment['quantity']
                assert quantity >= 0
                if not quantity:
                    continue
                width = 700 * quantity / denominator
                fill = '#64748b' if segment['owner'] == 'external' else '#2563eb' if segment['label'] != 'O2' else '#0f766e'
                pieces.append(f'<rect x="{x:.3f}" y="{y}" width="{width:.3f}" height="34" fill="{fill}" stroke="#fff"/>')
                if width >= 40:
                    pieces.append(text_lines([str(quantity)],round(x+8,3),y+24,16,'#fff',22,'600'))
                x += width
            labels = ' · '.join(f'{s["label"]} {s["quantity"]}' for s in queue[key] if s['quantity']) or '표시한 큐의 잔량 0'
            pieces.append(text_lines([labels],132,y+51,14,'#475569',20))
        pieces.append(text_lines([f'내 확정 체결 {queue["own_filled"]}개 / 다음 가격·후순위로 넘길 틱 예산 {queue["remaining_tick"]}개'],32,footer+151,16,'#0f172a',22,'600'))
        footer += 176
    pieces += [text_lines(['왼쪽에서 오른쪽으로 읽습니다. 수치는 조건을 설명하는 합성 예시이며 거래 성과 예측이 아닙니다.',
                          '현재 지원 상태와 세부 조건은 이 그림 아래의 본문·사례 대조표를 함께 확인하세요.'],32,footer,15,'#475569',24),
               '</g></svg>']
    result = '\n'.join(pieces)+'\n'
    ET.fromstring(result)
    return result


def render_mermaid(scene, status_key=None):
    # SVG is a portable rendering of this three-node Mermaid storyboard.
    labels = []
    for i, step in enumerate(scene['steps']):
        label = '<br/>'.join(escape(line, quote=True) for line in step)
        labels.append(f'    N{i}["{label}"]')
    connector = '-.->' if status_key == 'unsupported' else '-->'
    return f'%% {scene["id"]}: {scene["title"]}\nflowchart LR\n'+'\n'.join(labels)+f'\n    N0 {connector} N1\n    N1 {connector} N2\n'


def case_page(group, rows, scenes):
    parts = [f'# {group}. {GROUPS[group]} — 사례별 설명 그림\n',
             '[그림 안내](../visual_guide.ko.md) · [원본 사례 목록](../point2_limit_fill_cases.ko.md) · [현재 실행 범위](../limit_execution_coverage.ko.md)\n',
             '각 그림은 해당 조건에서의 흐름을 설명한다. **설계 제안·현재 실행 미지원** 표시는 향후 동작이며, 부분 지원은 전체 동작의 구현을 뜻하지 않는다. 원본 제안과 현행 동작이 다르면 그림 아래 구분을 따른다.\n',
             '모든 수치 예시는 명시한 선행 물량 외에 더 우선한 물량이 없고, 별도 언급이 없으면 지연·수수료·규격 제한을 생략한 합성 상황이다. 실제 체결 확률이나 성과 예측이 아니다.\n',
             ' | '.join(f'[{r["id"]}](#{r["id"].lower()})' for r in rows)+'\n']
    for row in rows:
        identifier = row['id']
        scene = scenes[identifier]
        steps = scene['steps']
        parts += [f'<a id="{identifier.lower()}"></a>\n\n## {identifier} · {row["scenario"]}\n',
                  f'**현재 상태: {STATUSES[row["status"]][0]}**\n',
                  f'![{identifier}: '+ ' → '.join(' / '.join(s) for s in steps)+f'](../figures/{identifier.lower()}.svg)\n',
                  '**그림 읽기:** '+ ' → '.join(' / '.join(s) for s in steps)+'.\n',
                  f'**원본 처리 제안:** {row["design"]}\n',
                  f'**현행 범위:** {row["note"]} 세부 정책은 [실행 범위 본문](../limit_execution_coverage.ko.md)을 따른다.\n',
                  f'<details>\n<summary>{identifier} Mermaid 원본 보기</summary>\n\n```mermaid\n{render_mermaid(scene,row["status"])}```\n\n</details>\n']
    return '\n'.join(parts)


def build():
    catalog = read_json(ROOT/'oms_client/replay/limit_case_matrix.json')
    supplied = read_json(FIGURES/'limit-scenes.json')
    assert [r['id'] for r in supplied] == [r['id'] for r in catalog], 'Case IDs must match the catalog exactly once and in order'
    sources = {r['id']: r for r in supplied}
    scenes, output = {}, {}
    for row in catalog:
        scene = dict(sources[row['id']], title=f'{row["id"]} · {row["scenario"]}')
        scenes[row['id']] = scene
        output[FIGURES/f'{row["id"].lower()}.svg'] = render_svg(scene,row['status'])
    for group in GROUPS:
        rows = [r for r in catalog if r['id'].startswith(group)]
        output[ROOT/f'docs/illustrated/limit-{group.lower()}.ko.md'] = case_page(group,rows,scenes)
    overview = read_json(FIGURES/'overview-scenes.json')
    assert len({r['id'] for r in overview}) == len(overview)
    for scene in overview:
        output[FIGURES/f'{scene["id"]}.svg'] = render_svg(scene)
        output[FIGURES/f'{scene["id"]}.mmd'] = render_mermaid(scene)
    counts = Counter(r['status'] for r in catalog)
    return output, dict(cases=len(catalog), overview=len(overview), groups=len(GROUPS), statuses=dict(counts))


def check_links():
    for path in (ROOT/'docs').rglob('*.md'):
        for target in re.findall(r'\]\(([^)]+)\)',path.read_text()):
            if 'figures/' not in target and 'illustrated/' not in target:
                continue
            filename, _, anchor = target.partition('#')
            dest = (path.parent/filename).resolve()
            assert dest.is_file(), f'Broken illustration link: {path}: {target}'
            if anchor:
                assert f'<a id="{anchor}">' in dest.read_text(), f'Missing anchor: {target}'
    for name in ('point2_limit_fill_cases.ko.md','limit_execution_coverage.ko.md'):
        content = (ROOT/'docs'/name).read_text()
        ids = re.findall(r'^\| \[([A-L]\d{2})\]\(illustrated/limit-[a-l]\.ko\.md#[a-l]\d{2}\)', content, re.M)
        expected = [r['id'] for r in read_json(ROOT/'oms_client/replay/limit_case_matrix.json')]
        assert ids == expected, f'Each case must have exactly one illustrated link in {name}'
    scenes = {r['id']: r for r in read_json(FIGURES/'overview-scenes.json')}
    bindings = read_json(FIGURES/'document-bindings.json')
    used = set()
    for filename, headings in bindings.items():
        content = (ROOT/'docs'/filename).read_text()
        for heading, identifiers in headings.items():
            assert heading+'\n' in content, f'Missing section: {filename}: {heading}'
            for identifier in identifiers:
                assert f'<!-- diagram: {identifier} -->' in content, f'Missing illustration: {filename}: {identifier}'
                assert scenes[identifier]['caption'] in content, f'Stale explanation: {filename}: {identifier}'
                used.add(identifier)
    assert used == set(scenes), 'All overview diagrams must be embedded in a source document'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args = parser.parse_args()
    output, summary = build()
    for path, content in output.items():
        if args.check:
            assert path.is_file() and path.read_text() == content, f'Stale artifact: {path}'
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(content)
    if args.check:
        check_links()
    print(json.dumps(dict(summary, artifacts=len(output), mode='check' if args.check else 'render'),ensure_ascii=False))


if __name__ == '__main__':
    main()
