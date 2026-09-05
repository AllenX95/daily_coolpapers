"""Optional browser + read-only SQLite acceptance against the isolated fixture."""
import argparse
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:18767')
    parser.add_argument('--db', required=True, help='Temporary DB printed by preview_investment_themes')
    parser.add_argument('--output', default='tmp/b-second-stage-browser-results')
    parser.add_argument('--channel', default='msedge')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = Path(args.db).resolve()

    def rows(sql):
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as conn:
            return conn.execute(sql).fetchall()

    assert rows('SELECT title FROM papers WHERE id=1') == [('Agent Memory: A Research Starting Point',)]
    assert rows('SELECT COUNT(*) FROM investment_themes') == [(0,)]
    baseline = {table: rows(f'SELECT * FROM {table} ORDER BY id')
                for table in ('papers', 'evaluations', 'jobs')}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=args.channel)
        context = browser.new_context(viewport={'width': 1440, 'height': 1050})
        context.route('**/*', lambda route: route.continue_() if urlsplit(route.request.url).netloc == urlsplit(args.url).netloc else route.abort())
        page = context.new_page()
        errors, posts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: posts.append((urlsplit(response.url).path, response.status))
                if response.request.method == 'POST' else None)

        def visit(path):
            response = page.goto(args.url+path)
            assert response is None or response.status == 200
            page.wait_for_load_state('networkidle')

        def click(button):
            button.click()
            page.wait_for_load_state('networkidle')

        def memberships(expected):
            assert rows('SELECT theme_id FROM paper_investment_themes WHERE paper_id=1 ORDER BY theme_id') == [(value,) for value in expected]

        visit('/investment-themes')
        print('Observed empty-state controls:', page.get_by_role('button').all_text_contents(), flush=True)
        assert page.get_by_test_id('theme-record').count() == 0
        page.screenshot(path=str(output/'themes-empty-desktop.png'), full_page=True)
        assert not errors, errors

        # Local paper return survives creation; creation itself never adds membership.
        visit('/papers/1#fulltext-result')
        panel = page.get_by_test_id('paper-themes')
        assert panel.is_visible()
        assert page.get_by_text('最新一次全文任务失败', exact=False).count() == 1
        click(panel.get_by_role('link', name='创建／管理主题'))
        create = page.locator('.theme-create')
        for name, description in [('Agent 基础设施', '企业 Agent 的长期记忆与工作流'), ('高效推理', '推理成本与模型效率')]:
            create.get_by_label('主题名称', exact=True).fill(name)
            create.get_by_label('主题说明（可选）', exact=True).fill(description)
            click(create.get_by_role('button', name='创建主题'))
            assert 'paper_id=1' in page.url
        memberships([])
        first = page.locator('#theme-1')
        click(first.locator('summary'))
        first.get_by_label('主题名称', exact=True).fill('Agent 长期记忆')
        click(first.get_by_role('button', name='保存修改'))
        assert rows('SELECT name FROM investment_themes WHERE id=1') == [('Agent 长期记忆',)]
        click(page.get_by_role('link', name='返回论文选择主题：Agent Memory: A Research Starting Point'))
        for theme in (1, 2):
            panel.locator(f'input[value="{theme}"]').check()
        click(panel.get_by_role('button', name='保存投资主题'))
        assert page.url.endswith('/papers/1#fulltext-result')
        memberships([1, 2])
        assert panel.locator('.assigned-themes li').count() == 2
        assert rows('SELECT COUNT(*) FROM paper_dispositions WHERE paper_id=1') == [(0,)]
        page.reload()
        page.wait_for_load_state('networkidle')
        assert panel.locator('input:checked').count() == 2
        assert panel.locator('.theme-choice').first.evaluate("e => getComputedStyle(e).flexDirection") == 'row'
        panel.screenshot(path=str(output/'paper-themes-desktop.png'))

        click(panel.get_by_role('link', name='Agent 长期记忆', exact=True))
        assert page.get_by_test_id('theme-papers').locator('.favorite-item').count() == 1
        for sort in ('score_desc', 'title', 'added_desc'):
            page.get_by_label('排序', exact=True).select_option(sort)
            click(page.get_by_role('button', name='应用', exact=True))
            assert page.locator('.favorite-item').count() == 1
        page.screenshot(path=str(output/'theme-papers-desktop.png'), full_page=True)
        visit('/favorites')
        assert page.locator('.favorite-item').count() == 0

        # Stale browser form must fail atomically after another tab archives the theme.
        visit('/papers/1#fulltext-result')
        other = context.new_page()
        other.goto(args.url+'/investment-themes')
        other.locator('#theme-1').get_by_role('button', name='归档主题').click()
        other.wait_for_load_state('networkidle')
        assert rows('SELECT status FROM investment_themes WHERE id=1') == [('archived',)]
        memberships([1, 2])
        click(panel.get_by_role('button', name='保存投资主题'))
        assert posts[-1][1] == 409
        memberships([1, 2])
        assert '刷新' in page.locator('body').inner_text()
        assert page.get_by_role('alert').is_visible()
        click(page.get_by_role('link', name='返回论文并刷新主题选项'))
        other.close()
        visit('/papers/1#fulltext-result')
        assert panel.locator('input[value="1"]').count() == 0
        assert panel.get_by_role('link', name='Agent 长期记忆 · 已归档').count() == 1
        panel.locator('input[value="2"]').uncheck()
        click(panel.get_by_role('button', name='保存投资主题'))
        memberships([1])
        click(panel.get_by_role('button', name='从论文移除主题：Agent 长期记忆'))
        memberships([])
        visit('/investment-themes')
        click(page.locator('#theme-1').get_by_role('button', name='恢复主题'))
        visit('/papers/1#fulltext-result')
        panel.locator('input[value="1"]').check()
        click(panel.get_by_role('button', name='保存投资主题'))
        memberships([1])
        click(page.get_by_test_id('personal-decision').get_by_role('button', name='跳过', exact=True))
        memberships([1])
        visit('/investment-themes/1/papers')
        assert page.locator('.favorite-item .decision-badge.skipped').count() == 1

        # Narrow viewport and long labels; no whole-page horizontal overflow.
        page.set_viewport_size({'width': 390, 'height': 844})
        visit('/investment-themes')
        first = page.locator('#theme-1')
        click(first.locator('summary'))
        first.get_by_label('主题名称', exact=True).fill('长期投资主题'+ 'X'*65)
        first.get_by_label('主题说明（可选）', exact=True).fill('研究边界与个人投资假设。'*35)
        click(first.get_by_role('button', name='保存修改'))
        for path, filename in [('/investment-themes', 'themes-mobile.png'), ('/papers/1#fulltext-result', 'paper-themes-mobile.png'), ('/investment-themes/1/papers', 'theme-papers-mobile.png')]:
            visit(path)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), path
            if '/papers/1#' in path:
                panel.screenshot(path=str(output/filename))
            else:
                page.screenshot(path=str(output/filename), full_page=True)
        visit('/papers/2')
        assert page.get_by_test_id('paper-themes').count() == 0
        assert all(status == 302 for _, status in posts if status != 409), posts
        assert sum(status == 409 for _, status in posts) == 1
        assert not errors, errors
        for table, expected in baseline.items():
            assert rows(f'SELECT * FROM {table} ORDER BY id') == expected, table
        print('PASS boundaries: UI forms -> POST 302/409 -> read-only SQLite -> refreshed theme cards/lists.', flush=True)
        print('Observed POSTs:', posts, flush=True)
        browser.close()
    print('B second-stage browser checks passed:', output, flush=True)


if __name__ == '__main__':
    main()
