"""Optional browser + read-only SQLite acceptance against the module D fixture."""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:18769')
    parser.add_argument('--db',required=True)
    parser.add_argument('--output',default='tmp/d-browser-results')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    database = Path(args.db).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    def rows(sql):
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as conn:
            return conn.execute(sql).fetchall()
    assert rows('SELECT title FROM papers WHERE id=1') == [('D Fixture 01 Memory',)]
    assert rows('SELECT COUNT(*) FROM attention_directions') == [(0,)]
    baseline = rows('SELECT * FROM papers')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge',headless=True)
        context = browser.new_context(viewport={'width':1440,'height':1050})
        context.route('**/*',lambda r:r.continue_() if urlsplit(r.request.url).netloc == urlsplit(args.url).netloc else r.abort())
        page = context.new_page()
        errors = []
        page.on('pageerror',lambda error:errors.append(str(error)))
        def visit(path):
            response = page.goto(args.url+path)
            assert response.status == 200
            page.wait_for_load_state('networkidle')
        def click(locator):
            locator.click()
            page.wait_for_load_state('networkidle')
        def paper_titles():
            return page.locator('.paper-title').all_text_contents()

        visit('/')
        assert len(paper_titles()) == 4
        assert page.get_by_text('当前未启用关注方向，将对全部新论文执行摘要评估。',exact=True).is_visible()
        visit('/attention-directions')
        assert page.get_by_role('heading',name='关注方向',exact=True).is_visible()
        page.screenshot(path=str(output/'settings-empty.png'),full_page=True)
        page.get_by_role('textbox',name='方向名称',exact=True).fill('Agent memory')
        page.get_by_role('textbox',name='范围简介',exact=True).fill('研究可持久化 Agent 记忆，不包含单纯产品包装。允许相邻基础技术进入待确认。')
        click(page.get_by_role('button',name='保存新方向'))
        assert rows('SELECT name,status FROM attention_directions') == [('Agent memory','active')]
        assert rows('SELECT COUNT(*) FROM jobs') == [(0,)]
        visit('/')
        assert paper_titles() == []
        page.get_by_role('combobox',name='阅读范围',exact=True).select_option('all')
        click(page.get_by_role('button',name='筛选',exact=True))
        assert len(paper_titles()) == 4
        visit('/attention-directions')
        click(page.get_by_text('手动补分类历史论文',exact=True))
        page.locator('input[name=date_from]').fill('2026-09-01')
        page.locator('input[name=date_to]').fill('2026-09-03')
        click(page.get_by_role('button',name='预览历史补分类'))
        assert page.get_by_role('heading',name='确认历史补分类',exact=True).is_visible()
        assert rows('SELECT COUNT(*) FROM jobs') == [(0,)]
        page.screenshot(path=str(output/'backfill-preview.png'),full_page=True)
        click(page.get_by_role('button',name='确认执行分类并自动补评'))
        job_id = rows('SELECT MAX(id) FROM jobs')[0][0]
        for _ in range(30):
            if rows(f'SELECT status FROM jobs WHERE id={job_id}') == [('partial_success',)]:
                break
            page.wait_for_timeout(150)
        assert rows(f'SELECT status FROM jobs WHERE id={job_id}') == [('partial_success',)]
        visit(f'/jobs/{job_id}?severity=all&view=timeline')
        assert page.get_by_role('heading',name='关注方向分类',exact=True).is_visible()
        assert rows("SELECT COUNT(*) FROM evaluations WHERE evaluation_type='abstract_review' AND status='success'") == [(2,)]
        assert rows('SELECT model_decision FROM paper_direction_results ORDER BY paper_id') == [('matched',),('possible',),('unmatched',),('failed',)]
        page.screenshot(path=str(output/'backfill-result.png'),full_page=True)
        print('PASS D1/D2/D4: create -> preview (no job) -> classification -> abstract -> partial status; database agrees.',flush=True)

        visit('/')
        assert paper_titles() == ['D Fixture 01 Memory','D Fixture 02 Adjacent']
        visit('/papers/2#paper-directions')
        panel = page.locator('#paper-directions')
        assert panel.get_by_text('待确认分类',exact=True).is_visible()
        click(panel.get_by_role('button',name='确认属于此方向'))
        assert rows('SELECT model_decision,manual_decision FROM paper_direction_results WHERE paper_id=2') == [('possible','confirmed')]
        visit('/papers/1#paper-directions')
        click(page.locator('#paper-directions').get_by_role('button',name='否决此方向／不相关'))
        visit('/')
        assert paper_titles() == ['D Fixture 02 Adjacent']
        page.get_by_role('combobox',name='模型分类',exact=True).select_option('unmatched')
        click(page.get_by_role('button',name='筛选',exact=True))
        assert paper_titles() == ['D Fixture 03 Other']
        visit('/papers/3#paper-directions')
        page.locator('#paper-directions select[name=direction_id]').select_option('1')
        click(page.locator('#paper-directions').get_by_role('button',name='确认加入关注方向'))
        click(page.get_by_role('button',name='手动摘要评估（已有结果时重新评估）'))
        for _ in range(30):
            if rows("SELECT COUNT(*) FROM evaluations WHERE paper_id=3 AND evaluation_type='abstract_review' AND status='success'") == [(1,)]:
                break
            page.wait_for_timeout(150)
        assert rows('SELECT model_decision,manual_decision FROM paper_direction_results WHERE paper_id=3') == [('unmatched','confirmed')]
        assert rows("SELECT COUNT(*) FROM evaluations WHERE paper_id=3 AND evaluation_type='abstract_review' AND status='success'") == [(1,)]
        visit('/')
        assert paper_titles() == ['D Fixture 02 Adjacent','D Fixture 03 Other']
        print('PASS D3: pending confirmation, matched veto, manual override, unmatched manual abstract; model judgments preserved.',flush=True)

        page.set_viewport_size({'width':390,'height':844})
        for path,name in [('/attention-directions','settings-mobile'),('/papers/2#paper-directions','paper-mobile'),(f'/jobs/{job_id}','job-mobile'),('/','index-mobile')]:
            visit(path)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1'),path
            page.screenshot(path=str(output/(name+'.png')),full_page=True)
        assert not errors,errors
        context.close()
        nojs = browser.new_context(java_script_enabled=False,viewport={'width':390,'height':844})
        nojs.route('**/*',lambda r:r.continue_() if urlsplit(r.request.url).netloc == urlsplit(args.url).netloc else r.abort())
        page = nojs.new_page()
        visit('/attention-directions')
        page.get_by_role('textbox',name='方向名称',exact=True).fill('No JS direction')
        page.get_by_role('textbox',name='范围简介',exact=True).fill('Native HTML works.')
        page.get_by_role('button',name='保存新方向').press('Enter')
        page.wait_for_load_state('networkidle')
        assert rows('SELECT COUNT(*) FROM attention_directions') == [(2,)]
        page.locator('#direction-2').get_by_role('button',name='归档方向（不可恢复）').press('Enter')
        page.wait_for_load_state('networkidle')
        assert rows('SELECT status FROM attention_directions WHERE id=2') == [('archived',)]
        assert rows('SELECT * FROM papers') == baseline
        assert rows('SELECT COUNT(*) FROM paper_dispositions') == [(0,)]
        assert rows('SELECT COUNT(*) FROM paper_investment_themes') == [(0,)]
        nojs.close()
        browser.close()
        print('PASS: 1440/390 layouts, no JS errors, native no-JS create/archive; metadata and personal library unchanged.',flush=True)


if __name__ == '__main__':
    main()
