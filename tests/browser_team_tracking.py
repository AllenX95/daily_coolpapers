"""Optional Edge/Playwright acceptance, only against preview_team_tracking."""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:18768')
    parser.add_argument('--db', required=True, help='Temporary DB printed by preview_team_tracking')
    parser.add_argument('--output', default='tmp/c-browser-results')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = Path(args.db).resolve()

    def rows(sql):
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as conn:
            return conn.execute(sql).fetchall()

    assert rows('SELECT title FROM papers WHERE id=1') == [('Agent Memory: Team Research Fixture',)]
    assert rows('SELECT COUNT(*) FROM research_authors') == [(0,)]
    baseline = {table: rows(f'SELECT * FROM {table}') for table in
                ('papers', 'evaluations', 'jobs', 'paper_dispositions', 'paper_investment_themes')}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel='msedge')
        context = browser.new_context(viewport={'width': 1440, 'height': 1050})
        context.route('**/*', lambda route: route.continue_() if urlsplit(route.request.url).netloc == urlsplit(args.url).netloc else route.abort())
        page = context.new_page()
        errors, posts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: posts.append((urlsplit(response.url).path, response.status)) if response.request.method == 'POST' else None)

        def visit(path):
            response = page.goto(args.url+path)
            assert response is None or response.status == 200
            page.wait_for_load_state('networkidle')

        def click(locator):
            locator.click()
            page.wait_for_load_state('networkidle')

        def editor():
            panel = page.get_by_test_id('paper-team')
            click(panel.locator('.team-editor > summary'))
            return panel

        visit('/research-entities')
        assert page.get_by_role('heading', name='研究对象', exact=True).is_visible()
        assert page.get_by_role('navigation', name='研究对象视图').get_by_role('link').count() == 3
        assert page.get_by_test_id('tracking-record').count() == 0
        page.screenshot(path=str(output/'empty-desktop.png'), full_page=True)
        print('Gut check PASS: research views, filters and empty state render; no JS errors.', flush=True)
        click(page.get_by_role('link', name='论文', exact=True))
        assert page.locator('main').inner_text()

        visit('/papers/1#fulltext-result')
        assert page.get_by_text('最新一次全文任务失败', exact=False).count() == 1
        panel = editor()
        assert panel.get_by_role('textbox', name='第一作者姓名', exact=True).input_value() == 'Ada'
        panel.get_by_role('textbox', name='第一作者姓名', exact=True).fill('Ada Confirmed')
        panel.get_by_role('textbox', name='机构名称', exact=True).fill('Memory Lab')
        panel.get_by_role('combobox', name='机构类型', exact=True).select_option('company')
        panel.get_by_role('textbox', name='国家/地区（可选）', exact=True).fill('中国')
        panel.get_by_role('textbox', name='论文跟踪备注（可选）', exact=True).fill('Long horizon research')
        panel.screenshot(path=str(output/'entry-desktop.png'))
        click(panel.get_by_role('button', name='确认并保存团队跟踪'))
        assert page.url.endswith('/papers/1#fulltext-result')
        assert rows('SELECT lead_author_id,organization_id,status FROM paper_team_tracking WHERE paper_id=1') == [(1, 1, 'tracking')]
        identity = rows('SELECT id,created_at FROM paper_team_tracking WHERE paper_id=1')
        page.reload()
        page.wait_for_load_state('networkidle')
        assert page.get_by_test_id('paper-team').get_by_text('Ada Confirmed', exact=True).is_visible()
        click(page.get_by_test_id('paper-team').get_by_role('button', name='停止跟踪'))
        assert rows('SELECT status FROM paper_team_tracking WHERE paper_id=1') == [('archived',)]
        panel = editor()
        assert panel.get_by_role('combobox', name='已有作者', exact=True).input_value() == '1'
        click(panel.get_by_role('button', name='确认并保存团队跟踪'))
        assert rows('SELECT id,created_at FROM paper_team_tracking WHERE paper_id=1') == identity

        # Explicit duplicate resolution preserves the remaining draft across both conflicts.
        visit('/papers/2#fulltext-result')
        panel = editor()
        panel.get_by_role('radio', name='新建作者', exact=True).check()
        panel.get_by_role('radio', name='新建机构', exact=True).check()
        panel.get_by_role('textbox', name='第一作者姓名', exact=True).fill('ＡＤＡ　ＣＯＮＦＩＲＭＥＤ')
        panel.get_by_role('textbox', name='机构名称', exact=True).fill('MEMORY LAB')
        panel.get_by_role('combobox', name='机构类型', exact=True).select_option('company')
        panel.get_by_role('textbox', name='论文跟踪备注（可选）', exact=True).fill('Preserve conflict draft')
        click(panel.get_by_role('button', name='确认并保存团队跟踪'))
        assert posts[-1][1] == 409
        assert page.get_by_test_id('entity-conflict').count() == 2
        assert rows('SELECT COUNT(*) FROM paper_team_tracking') == [(1,)]
        page.screenshot(path=str(output/'conflicts-desktop.png'), full_page=True)
        click(page.get_by_role('button', name='使用该作者记录并重试'))
        assert posts[-1][1] == 409
        click(page.get_by_role('button', name='使用该机构记录并重试'))
        assert rows('SELECT lead_author_id,organization_id,notes FROM paper_team_tracking WHERE paper_id=2') == [(1, 1, 'Preserve conflict draft')]
        panel = editor()
        panel.get_by_role('radio', name='新建机构', exact=True).check()
        panel.get_by_role('textbox', name='机构名称', exact=True).fill('Reasoning Institute')
        panel.get_by_role('combobox', name='机构类型', exact=True).select_option('research_institute')
        click(panel.get_by_role('button', name='确认并保存团队跟踪'))
        assert rows('SELECT paper_id,organization_id FROM paper_team_tracking ORDER BY paper_id') == [(1, 1), (2, 2)]

        visit('/research-entities?view=authors')
        author = page.locator('#author-1')
        assert '2 家关联机构' in author.locator('.research-counts').inner_text()
        click(author.locator('.research-counts a'))
        assert page.get_by_test_id('tracking-record').count() == 2
        visit('/research-entities?view=authors')
        author = page.locator('#author-1')
        click(author.locator('summary'))
        author.get_by_role('textbox', name='名称', exact=True).fill('Ada Researcher')
        author.get_by_role('combobox', name='作者分类', exact=True).select_option('hybrid')
        author.get_by_role('textbox', name='备注（可选）', exact=True).fill('Manually checked identity')
        click(author.get_by_role('button', name='保存实体修改'))
        assert rows('SELECT name,author_category FROM research_authors') == [('Ada Researcher', 'hybrid')]
        page.screenshot(path=str(output/'authors-desktop.png'), full_page=True)

        # Entity archive must not stop tracking; restoration is a separate explicit POST.
        visit('/research-entities?view=organizations')
        click(page.locator('#organization-1').get_by_role('button', name='归档实体'))
        assert rows('SELECT status FROM paper_team_tracking WHERE paper_id=1') == [('tracking',)]
        visit('/papers/1#fulltext-result')
        panel = editor()
        assert '机构已归档' in panel.inner_text()
        click(panel.get_by_role('button', name='确认并保存团队跟踪'))
        assert posts[-1][1] == 409
        team_before = rows('SELECT * FROM paper_team_tracking')
        click(page.get_by_role('button', name='恢复该机构记录'))
        assert rows('SELECT * FROM paper_team_tracking') == team_before
        assert 'team_organization_id=1' in page.url
        panel = editor()
        click(panel.get_by_role('button', name='确认并保存团队跟踪'))
        visit('/papers/2#fulltext-result')
        click(page.get_by_test_id('paper-team').get_by_role('button', name='停止跟踪'))

        visit('/research-entities')
        page.get_by_role('textbox', name='名称搜索', exact=True).fill('ADA RESEARCHER')
        page.get_by_role('combobox', name='作者分类筛选').select_option('hybrid')
        page.get_by_role('combobox', name='机构类型筛选').select_option('company')
        click(page.get_by_role('button', name='应用筛选'))
        assert page.get_by_test_id('tracking-record').count() == 1
        visit('/research-entities?status=archived')
        assert page.get_by_test_id('tracking-record').count() == 1

        # Long user-controlled names/notes and responsive layouts.
        page.set_viewport_size({'width': 390, 'height': 844})
        visit('/research-entities?view=organizations')
        organization = page.locator('#organization-1')
        click(organization.locator('summary'))
        organization.get_by_role('textbox', name='名称', exact=True).fill('长期研究机构'+'X'*70)
        organization.get_by_role('textbox', name='国家/地区（可选）', exact=True).fill('中国 / Singapore')
        organization.get_by_role('textbox', name='备注（可选）', exact=True).fill('仅用于隔离产品验收。'*20)
        click(organization.get_by_role('button', name='保存实体修改'))
        for path, filename in [('/research-entities?view=organizations', 'organizations-mobile.png'),
                               ('/research-entities?view=authors', 'authors-mobile.png'),
                               ('/research-entities?status=all', 'tracking-mobile.png'),
                               ('/papers/1#fulltext-result', 'paper-team-mobile.png')]:
            visit(path)
            if path.startswith('/papers/'):
                editor()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), path
            page.screenshot(path=str(output/filename), full_page=True)
        visit('/papers/3')
        assert page.get_by_test_id('paper-team').count() == 0

        no_js = browser.new_context(java_script_enabled=False)
        no_js.route('**/*', lambda route: route.continue_() if urlsplit(route.request.url).netloc == urlsplit(args.url).netloc else route.abort())
        plain = no_js.new_page()
        plain.goto(args.url+'/papers/1#fulltext-result')
        plain.wait_for_load_state('networkidle')
        plain.bring_to_front()
        plain.locator('.team-editor > summary').press('Enter')
        assert plain.get_by_test_id('paper-team').get_by_role('combobox', name='已有作者', exact=True).is_visible()
        plain.get_by_role('button', name='确认并保存团队跟踪').press('Enter')
        plain.wait_for_load_state('networkidle')
        assert plain.url.endswith('/papers/1#fulltext-result')
        no_js.close()
        for table, expected in baseline.items():
            assert rows(f'SELECT * FROM {table}') == expected, table
        assert all(status in (302, 409) for _, status in posts), posts
        assert sum(status == 409 for _, status in posts) == 3
        assert not errors, errors
        print('PASS: browser forms -> 302/409 -> temporary SQLite -> entity libraries / paper history.', flush=True)
        print('POST evidence:', posts, flush=True)
        browser.close()
    print('Module C browser acceptance passed:', output, flush=True)


if __name__ == '__main__':
    main()
