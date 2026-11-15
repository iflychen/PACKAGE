"""Bridge OCR documents to the existing review table; no SPC schema changes."""
from __future__ import annotations
import shutil
from pathlib import Path
import json
from datetime import datetime
from fastapi import HTTPException
import app as review

MARKERS = {'unknown', 'unclear', '辨識失敗', '未讀到'}

def text(value):
    return '' if value is None else str(value).strip()

def unresolved(value):
    return text(value).lower() in MARKERS

def init():
    review.init_review_db()
    with review.get_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS review_documents (
            token TEXT PRIMARY KEY, original_json TEXT NOT NULL,
            layout_json TEXT NOT NULL, corrected_json TEXT)''')

def source(token):
    init()
    with review.get_db() as db:
        row = db.execute('SELECT * FROM review_documents WHERE token=?', (token,)).fetchone()
    return dict(row) if row else None

def create_document_review(document, file_name, source_path=None):
    """Snapshot all pages, preserving missing keys, value types and page identities."""
    if not isinstance(document, dict) or not isinstance(document.get('pages'), list):
        raise ValueError('辨識結果缺少 pages，不能建立修正表格')
    columns = []
    locations = []
    records = []
    for pi, page in enumerate(document['pages']):
        for ri, row in enumerate(page.get('rows') or []):
            if not isinstance(row, dict):
                raise ValueError('辨識列不是物件，無法安全建立修正表格')
            for key in row:
                if key not in columns:
                    columns.append(key)
            locations.append([pi, ri])
            records.append(row)
    if not records:
        raise ValueError('沒有辨識到資料列，請查看 pipelines 的錯誤紀錄')
    # Page is a locked display column. Never write it back to source rows.
    page_label = '來源頁碼'
    while page_label in columns:
        page_label = '_' + page_label
    rows = [[document['pages'][pi].get('page_number', pi + 1)] +
            [row.get(key) for key in columns]
            for (pi, _), row in zip(locations, records)]
    metadata = document.get('metadata') or {}
    init()
    token = review.create_review_task(file_name, metadata, [page_label] + columns, rows)
    layout = {'locations': locations, 'keys': columns}
    with review.get_db() as db:
        db.execute('INSERT INTO review_documents(token,original_json,layout_json) VALUES(?,?,?)',
                   (token, json.dumps(document, ensure_ascii=False), json.dumps(layout, ensure_ascii=False)))
    if source_path is not None:
        original_path = Path(source_path)
        original_dir = review.DB_PATH.parent / 'review_originals'
        original_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original_path, original_dir / (token + original_path.suffix.lower()))
    count = sum(unresolved(v) for row in records for v in row.values()) + sum(unresolved(v) for v in metadata.values())
    return {'review_token': token, 'review_path': '/review/' + token,
            'review_url': 'http://localhost:8000/review/' + token,
            'review_pending_count': count, 'review_status': 'pending'}

def converted(value, original):
    v = text(value)
    if not v or v.lower() == 'null':
        return None
    if v == text(original):
        return original
    # Keep measured values as written (including leading zeros and ±).
    return v

def confirm(token, payload, task, record):
    document = json.loads(record['original_json'])
    layout = json.loads(record['layout_json'])
    original_rows = json.loads(task['original_rows_json'])
    columns = json.loads(task['columns_json'])
    original_meta = json.loads(task['metadata_json'])
    if payload.columns != columns or len(payload.rows) != len(original_rows):
        raise HTTPException(422, '請保留原有欄位和列數，避免修正值對錯位置')
    if set(payload.metadata) != set(original_meta):
        raise HTTPException(422, '請保留原有基本資料欄位')
    corrected_rows = []
    for i, row in enumerate(payload.rows):
        if len(row) != len(columns) or text(row[0]) != text(original_rows[i][0]):
            raise HTTPException(422, '頁碼或欄位數不一致')
        corrected = [original_rows[i][0]]
        pi, ri = layout['locations'][i]
        target = document['pages'][pi]['rows'][ri]
        for j, key in enumerate(layout['keys'], 1):
            value = converted(row[j], target.get(key))
            if unresolved(value):
                raise HTTPException(422, f'第 {i+1} 列「{key}」仍需修正；確定空白請清空或填 null')
            if key in target or text(row[j]):
                target[key] = value
            corrected.append(value)
        corrected_rows.append(corrected)
    metadata = {k: converted(v, original_meta[k]) for k, v in payload.metadata.items()}
    if any(unresolved(v) for v in metadata.values()):
        raise HTTPException(422, '基本資料仍有 unknown / unclear，請修正或確認空白')
    document['metadata'] = metadata
    for page in document['pages']:
        if isinstance(page.get('metadata'), dict):
            for key in page['metadata']:
                if key in metadata:
                    page['metadata'][key] = metadata[key]
    now = datetime.now().isoformat(timespec='seconds')
    document['review'] = {'status': 'confirmed', 'confirmed_at': now, 'token': token,
                          'storage': 'review_tasks.db', 'spc_synced': False}
    # file_content also contains a document copy in older output: keep it consistent.
    if isinstance(document.get('file_info'), dict) and 'file_content' in document['file_info']:
        document['file_info']['file_content'] = json.dumps(
            {'metadata': metadata, 'pages': document['pages']}, ensure_ascii=False)
    dump = lambda v: json.dumps(v, ensure_ascii=False)
    with review.get_db() as db:
        result = db.execute('''UPDATE review_tasks SET corrected_metadata_json=?,
            corrected_columns_json=?,corrected_rows_json=?,status='confirmed',confirmed_at=?
            WHERE token=? AND status!='confirmed' ''',
            (dump(metadata), dump(columns), dump(corrected_rows), now, token))
        if result.rowcount != 1:
            raise HTTPException(409, '這份檢表已經確認過，請重新開放編輯')
        db.execute('UPDATE review_documents SET corrected_json=? WHERE token=?', (dump(document), token))
    return {'status': 'confirmed', 'token': token, 'metadata': metadata, 'columns': columns,
            'rows': corrected_rows, 'export_url': f'/api/reviews/{token}/export', 'spc_synced': False}

def enhance_html(page, token, task, record):
    document = json.loads(record['original_json'])
    warnings = [str(error) for p in document['pages'] for error in (p.get('errors') or [])]
    config = json.dumps({'token': token, 'status': task['status'], 'warnings': warnings}, ensure_ascii=False).replace('<', '\\u003c')
    script = UI.replace('__CONFIG__', config)
    if original_file(token):
        script = script.replace('下載目前 JSON</a>', '下載目前 JSON</a> ｜ <a href="/api/reviews/' + token + '/original" target="_blank" rel="noopener">開啟原始檢表</a>')
    return page.replace('</body>', script + '</body>')

def original_file(token):
    if not token or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in token):
        return None
    folder = review.DB_PATH.parent / 'review_originals'
    for suffix in ('.pdf', '.png', '.jpg', '.jpeg', '.webp'):
        path = folder / (token + suffix)
        if path.is_file():
            return path
    return None

UI = r'''
<style>
.review-uncertain {background:#fff0b3!important;outline:2px solid #d89a00!important;}
.review-empty:empty::before {content:'null（空白）';color:#858585;pointer-events:none;}
#reviewHelp {padding:16px;border:1px solid #aac4df;background:#f4f9ff;margin:16px;border-radius:8px;line-height:1.8;}
#reviewHelp button {margin:4px;padding:6px 12px;cursor:pointer;}
</style>
<script>
(() => {
const config = __CONFIG__;
const panel = document.createElement('section'); panel.id='reviewHelp';
panel.innerHTML='<strong>人工修正</strong><div>黃色：unknown / unclear / 辨識失敗，請核對後修正。空白以 null 保存，不會強迫猜值。一般格子也能更正。</div><div>送出後保存到本專案的人工修正資料庫；尚未同步 SPC 測量資料。</div><span id="pendingCount"></span> <button type="button" id="nextPending">下一個待修正</button><button type="button" id="filterPending">只看待修正列</button> <a id="exportResult">下載目前 JSON</a><div id="reviewWarnings"></div>';
document.body.prepend(panel);
document.getElementById('exportResult').href='/api/reviews/'+config.token+'/export';
document.getElementById('reviewWarnings').textContent=config.warnings.length ? '辨識警告（請核對原檔是否漏列）：'+config.warnings.join('；') : '';
const structural=['addMetaBtn','deleteMetaBtn','addRowAboveBtn','addRowBelowBtn','addColumnLeftBtn','addColumnRightBtn','deleteRowBtn','deleteColumnBtn'];
structural.forEach(id=>{const b=document.getElementById(id);if(b){b.disabled=true;b.style.display='none';}});
currentHeaders().forEach(c=>c.contentEditable='false');
currentMetaPairs().forEach(p=>{if(p.key)p.key.contentEditable='false';});
currentRows().forEach(r=>{if(r.cells[0])r.cells[0].contentEditable='false';});
const cells=[...currentMetaPairs().map(p=>p.value).filter(Boolean),...currentRows().flatMap(r=>Array.from(r.cells).slice(1))];
const markers=new Set(['unknown','unclear','辨識失敗','未讀到']);
let filtered=false;
function refresh(){
 cells.forEach(c=>{const v=c.innerText.trim().toLowerCase();c.classList.toggle('review-uncertain',markers.has(v));c.classList.toggle('review-empty',!v||v==='null');});
 const pending=cells.filter(c=>c.classList.contains('review-uncertain'));
 document.getElementById('pendingCount').textContent='待修正 '+pending.length+' 格';
 currentRows().forEach(r=>r.style.display=filtered&&!r.querySelector('.review-uncertain')?'none':'');
}
cells.forEach(c=>c.addEventListener('input',refresh));
document.getElementById('nextPending').onclick=()=>{const c=cells.find(c=>c.classList.contains('review-uncertain'));if(c){c.scrollIntoView({block:'center',behavior:'smooth'});c.focus();}};
document.getElementById('filterPending').onclick=e=>{filtered=!filtered;e.target.textContent=filtered?'顯示全部列':'只看待修正列';refresh();};
confirmBtn.addEventListener('click',e=>{refresh();if(cells.some(c=>c.classList.contains('review-uncertain'))){e.stopImmediatePropagation();alert('仍有黃色格子未修正。請填入正確值，確定未填寫則清空或填 null。');}},true);
successBox.textContent='已確認並保存人工修正結果。可下載完整 JSON；尚未同步 SPC 測量資料。';
refresh();
})();
</script>
'''
