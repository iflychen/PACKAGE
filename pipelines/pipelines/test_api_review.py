"""Runs /process handler with a fake OCR result; no Ollama or database networking."""
from pathlib import Path
from tempfile import TemporaryDirectory
from io import BytesIO
import json,sys,os
from starlette.datastructures import UploadFile

sys.path.insert(0,str(Path(__file__).resolve().parent))
os.environ['ANIKI_API_KEY']='test-key'
import app as review
import aniki_api as api

with TemporaryDirectory() as tmp:
    review.DB_PATH=Path(tmp)/'review_tasks.db'
    api.UPLOAD_DIR=Path(tmp)
    api.ANIKI_SCRIPT=Path(tmp)/'Aniki.py'
    api.ANIKI_SCRIPT.touch()
    api.RESULT_PATH=Path(tmp)/'all_pages_result.json'
    data={'metadata':{'品號':'CMM2026'}, 'processing_mode':'original_cmm', 'pages':[
        {'page_number':1,'headers':['項目','实际值','名义值','上公差','下公差','偏差'],
         'rows':[{'項目':'直径_圆1','实际值':'0.0000','名义值':'0.0000','上公差':'0.1000','下公差':None,'偏差':'0.0000'}]},
        {'page_number':2,'headers':['項目','实际值','名义值','上公差','下公差','偏差'],
         'rows':[{'項目':'直径_圆2','实际值':'0.0090','名义值':'0.0000','上公差':'0.1000','下公差':'-0.1000','偏差':'0.0090'}]}
    ]}
    def run(command,env):
        api.RESULT_PATH.write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')
        return (0,'文件處理模式：original_cmm\n全部頁面讀取完成')
    api.run_aniki_process=run
    f=UploadFile(filename='sample_cmm.pdf',file=BytesIO(b'fake-pdf'))
    resp=api._process_single_report(f,'1 2',False,None,'test-key')
    assert resp['success'] and resp['status']=='pending_review',resp
    assert resp['review_path'].startswith('/review/') and resp['row_count']==2,resp
    task=review.get_review_task(resp['review_token'])
    cols=json.loads(task['columns_json'])
    rows=json.loads(task['original_rows_json'])
    assert cols[0]=='頁碼' and rows[1][0]=='2' and rows[0][2]=='0.0000',(cols,rows)
    assert task['status']=='pending'
    assert list(Path(tmp).glob('*.db'))

    generic_data={'metadata':{'品號':'GEN2026'}, 'processing_mode':'generic', 'pages':[
        {'page_number':1,'headers':['檢查項目','首件','1','2'],
         'rows':[{'檢查項目':'外徑','首件':'✓','1':'unknown','2':None}]}
    ]}
    def run_generic(command,env):
        api.RESULT_PATH.write_text(json.dumps(generic_data,ensure_ascii=False),encoding='utf8')
        return (0,'文件處理模式：generic\n全部頁面讀取完成')
    api.run_aniki_process=run_generic
    generic_file=UploadFile(filename='sample_generic.pdf',file=BytesIO(b'fake-pdf'))
    generic_resp=api._process_single_report(generic_file,'1',False,None,'test-key')
    assert generic_resp['success'] and generic_resp['document_type']=='generic',generic_resp
    assert generic_resp['review_path'].startswith('/review/'),generic_resp
    generic_task=review.get_review_task(generic_resp['review_token'])
    generic_rows=json.loads(generic_task['original_rows_json'])
    assert generic_rows[0][2]=='✓' and generic_rows[0][3]=='unknown' and generic_rows[0][4] is None,generic_rows
print('PASS: 2-page CMM OCR => one SQLite review task => pending review_path')
print('PASS: generic handwritten form => same SQLite review task flow')
