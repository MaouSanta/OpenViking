"""DWG chunks survive real unpack/vector-message/provider adaptation without merging."""
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from openviking.parse.output import LocalParseOutputStore, read_artifact_manifest
from openviking.parse.understanding_api import UnderstandingAPI
from openviking.models.embedder.volcengine_embedders import VolcengineDenseEmbedder
from openviking.utils import embedding_utils
from openviking.utils.embedding_input import estimate_embedding_input_tokens
from tests.unit.test_vectorize_file_strategy import DummyFS, DummyQueue, DummyQueueManager, DummyReq

MARKER='<!-- dwg-sheet-chunk:v1 -->'


@pytest.mark.asyncio
async def test_dwg_chunk_files_remain_independent_until_provider_payload(tmp_path,monkeypatch):
    contents={f'sheet-0001.part-{i:03d}.md':f'{MARKER}\n# sheet-0001 · part-{i:03d}\n\n'+f'第{i}块独立控制说明。'*40 for i in (1,2)}
    archive=tmp_path/'artifact.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for name,text in contents.items():z.writestr('drawing/sheets/'+name,text)
        z.writestr('drawing/sheet-manifest.json',json.dumps({'pages':[{'sheet_id':'sheet-0001','markdown':'sheets/'+next(iter(contents)),'md_paths':['sheets/'+n for n in contents]}]}))
    api=UnderstandingAPI.__new__(UnderstandingAPI);store=LocalParseOutputStore(str(tmp_path/'store'))
    ref=await api._unpack_zip_to_temp_dir(archive,'drawing',parse_output_store=store)
    manifest=await read_artifact_manifest(store,ref)
    assert {n for n in manifest if n.endswith('.md')}=={'drawing/sheets/'+n for n in contents}
    queue=DummyQueue();fs=DummyFS('must not read a concatenation')
    monkeypatch.setattr(embedding_utils,'get_queue_manager',lambda:DummyQueueManager(queue))
    monkeypatch.setattr(embedding_utils,'get_viking_fs',lambda:fs)
    monkeypatch.setattr(embedding_utils,'get_openviking_config',lambda:SimpleNamespace(embedding=SimpleNamespace(text_source='content_only',max_input_tokens=1000)))
    for name in contents:
        data=Path(ref.root,'drawing','sheets',name).read_bytes()
        await embedding_utils.vectorize_file(file_path='viking://user/default/resources/drawing/sheets/'+name,summary_dict={'name':name,'summary':'unused abstract'},parent_uri='viking://user/default/resources/drawing/sheets',ctx=DummyReq(),file_content=data)
    sent=[]
    def create(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(data=SimpleNamespace(embedding=[.1,.2,.3]),usage=SimpleNamespace(prompt_tokens=1,total_tokens=1))
    monkeypatch.setattr('volcenginesdkarkruntime.Ark',lambda **kwargs:SimpleNamespace(multimodal_embeddings=SimpleNamespace(create=create)))
    embedder=VolcengineDenseEmbedder(model_name='doubao-embedding-vision-251215',api_key='test-only',dimension=3,config={'max_input_tokens':1000,'max_retries':0})
    for message in queue.items:
        prepared=embedder.prepare_embedding_input(message.message)
        assert prepared==message.message
        embedder.embed(prepared)
    assert len(sent)==2
    assert [x['input'][0]['text'] for x in sent]==list(contents.values())
    assert all(estimate_embedding_input_tokens(x['input'][0]['text'])<=1000 for x in sent)
    assert all('truncated for embedding' not in x['input'][0]['text'] for x in sent)


@pytest.mark.asyncio
async def test_marked_dwg_chunk_fails_before_silent_truncation_but_legacy_keeps_behavior(monkeypatch):
    queue=DummyQueue();fs=DummyFS('unused')
    monkeypatch.setattr(embedding_utils,'get_queue_manager',lambda:DummyQueueManager(queue))
    monkeypatch.setattr(embedding_utils,'get_viking_fs',lambda:fs)
    monkeypatch.setattr(embedding_utils,'get_openviking_config',lambda:SimpleNamespace(embedding=SimpleNamespace(text_source='content_only',max_input_tokens=100)))
    async def vectorize(text):
        return await embedding_utils.vectorize_file(file_path='viking://user/default/resources/sheet.md',summary_dict={'name':'sheet.md','summary':''},parent_uri='viking://user/default/resources',ctx=DummyReq(),file_content=text.encode())
    with pytest.raises(ValueError,match='rebuild chunks'):
        await vectorize(MARKER+'\n'+'中文'*1000)
    assert not queue.items
    await vectorize('中文'*1000)
    assert len(queue.items)==1 and 'truncated for embedding' in queue.items[0].message
