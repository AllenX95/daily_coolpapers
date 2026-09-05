"""Browser -> POST -> mocked worker -> read-only SQLite memo acceptance."""
import argparse
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:18770')
    parser.add_argument('--db',required=True)
    parser.add_argument('--output',default='tmp/e-browser-results')
    parser.add_argument('--channel',default='msedge')
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright
    output=Path(args.output).resolve(); output.mkdir(parents=True,exist_ok=True)
    database=Path(args.db).resolve()
    def rows(sql,params=()):
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as conn:
            return conn.execute(sql,params).fetchall()
    assert rows("SELECT COUNT(*) FROM paper_dispositions WHERE decision='favorite'")==[(3,)]
    baseline={'papers':rows('SELECT * FROM papers ORDER BY id'),'evaluations':rows('SELECT * FROM evaluations ORDER BY id')}
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,channel=args.channel)
        context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
        origin=urlsplit(args.url).netloc
        context.route('**/*',lambda route:route.continue_() if urlsplit(route.request.url).netloc==origin else route.abort())
        page=context.new_page(); errors=[]; posts=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('response',lambda response:posts.append((urlsplit(response.url).path,response.status)) if response.request.method=='POST' else None)
        def visit(path):
            response=page.goto(args.url+path); assert response is None or response.status==200,(path,response.status)
            page.wait_for_load_state('networkidle')
        def submit(button):
            button.press('Enter'); page.wait_for_load_state('networkidle')
        def await_success():
            for _ in range(50):
                if 'success' in page.locator('body').inner_text(): return
                time.sleep(.1); page.reload(); page.wait_for_load_state('networkidle')
            raise AssertionError('memo did not reach success')

        visit('/investment-memos/new?source_mode=attention_direction&source_direction_id=1')
        assert page.get_by_text('Agent 基础设施',exact=True).count()>=1
        assert rows('SELECT COUNT(*) FROM jobs')==[(0,)]
        page.get_by_label('系列标题（保存后不可修改）').fill('Agent Infrastructure Thesis')
        candidates=page.locator('[data-testid="memo-candidate"]')
        assert candidates.count()==3
        candidates.nth(1).locator('input[type=checkbox]').check()
        submit(page.get_by_role('button',name='预览输入与调用预算'))
        body=page.locator('body').inner_text()
        assert '02 / confirm frozen evidence' in body.casefold(),body[:1200]
        assert rows('SELECT COUNT(*) FROM jobs')==[(0,)]
        page.screenshot(path=str(output/'memo-preview-desktop.png'),full_page=True)
        submit(page.get_by_role('button',name='确认生成新版本（一次调用）'))
        await_success()
        assert rows('SELECT status,input_tokens,output_tokens FROM investment_memo_versions')==[('success',321,123)]
        assert rows('SELECT COUNT(*) FROM investment_memo_version_papers')==[(2,)]
        assert rows('SELECT COUNT(*) FROM job_events WHERE event_type LIKE "investment_memo.%"')==[(3,)]
        assert page.locator('.memo-section').count()==12
        page.get_by_label('个人判断 Markdown').fill('## 我的结论\n\n- 产品机会待客户访谈验证')
        submit(page.get_by_role('button',name='保存我的投资判断'))
        assert rows('SELECT personal_judgment_markdown FROM investment_memo_versions')[0][0].startswith('## 我的结论')
        assert rows('SELECT COUNT(*) FROM jobs')==[(1,)]
        with page.expect_download() as download_info:
            page.get_by_role('link',name='导出 Markdown').click()
        download=download_info.value
        exported=Path(download.path()).read_text(encoding='utf-8')
        positions=[exported.index(token) for token in ('本备忘录仅基于','## 1. 核心结论','## 我的投资判断','## 13. 论文证据索引','## 生成配置摘要')]
        assert positions==sorted(positions)

        page.get_by_role('link',name='基于此版本创建新版本').click(); page.wait_for_load_state('networkidle')
        assert page.locator('input[name=paper_ids]:checked').count()==2
        page.get_by_label('复制所选旧版本的“我的投资判断”（不发送给模型）').check()
        submit(page.get_by_role('button',name='预览输入与调用预算'))
        submit(page.get_by_role('button',name='确认生成新版本（一次调用）'))
        await_success()
        assert rows('SELECT version_no,status FROM investment_memo_versions ORDER BY version_no')==[(1,'success'),(2,'success')]
        assert rows('SELECT personal_judgment_markdown FROM investment_memo_versions WHERE version_no=2')[0][0].startswith('## 我的结论')
        assert '我的结论' not in rows('SELECT input_snapshot_json FROM investment_memo_versions WHERE version_no=2')[0][0]
        page.get_by_role('link',name='版本历史 →').click(); page.wait_for_load_state('networkidle')
        submit(page.get_by_role('button',name='归档系列（不可恢复）'))
        assert rows('SELECT status FROM investment_memo_series')==[('archived',)]
        assert '不可恢复或新增版本' in page.locator('body').inner_text()
        page.screenshot(path=str(output/'memo-archived-desktop.png'),full_page=True)

        page.set_viewport_size({'width':390,'height':844})
        visit('/investment-memos?archived=1')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        page.screenshot(path=str(output/'memo-list-mobile.png'),full_page=True)
        visit('/investment-memos/1/versions/2')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        page.screenshot(path=str(output/'memo-version-mobile.png'),full_page=True)
        assert not errors,errors
        assert baseline['papers']==rows('SELECT * FROM papers ORDER BY id')
        assert baseline['evaluations']==rows('SELECT * FROM evaluations ORDER BY id')
        assert sum(path=='/investment-memos' for path,status in posts)==2,posts

        nojs=browser.new_context(viewport={'width':390,'height':844},java_script_enabled=False)
        nojs_page=nojs.new_page()
        response=nojs_page.goto(args.url+'/investment-memos?archived=1')
        assert response.status==200 and 'Agent Infrastructure Thesis' in nojs_page.locator('body').inner_text()
        assert nojs_page.locator('body').evaluate('(e)=>e.scrollWidth')<=390
        nojs.close(); context.close(); browser.close()
    print('PASS E browser: forms -> preview zero writes -> one-call jobs -> readonly DB -> versions/judgment/export/archive.',flush=True)
    print('Observed POSTs:',posts,flush=True)


if __name__=='__main__':
    main()
