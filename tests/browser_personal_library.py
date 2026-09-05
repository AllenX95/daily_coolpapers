"""Optional Playwright checks against preview_personal_library's temporary DB."""
import argparse
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:18766')
    parser.add_argument('--output', default='tmp/b-first-stage-browser-results')
    parser.add_argument('--channel', default='msedge')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=args.channel)
        context = browser.new_context(viewport={'width': 1440, 'height': 1050})
        context.route('**/*', lambda route: route.continue_() if urlsplit(route.request.url).netloc == urlsplit(args.url).netloc else route.abort())
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def visit(path):
            response = page.goto(args.url+path)
            assert response is None or response.status == 200  # Fragment-only navigation has no HTTP response.
            page.wait_for_load_state('networkidle')

        visit('/favorites')
        print('Observed navigation:', page.get_by_role('navigation', name='主导航').all_text_contents(), flush=True)
        assert page.locator('.favorite-item').count() == 0
        page.get_by_role('link', name='全文库', exact=True).click()
        page.wait_for_load_state('networkidle')
        assert page.get_by_role('heading', name='全文库', exact=True).count() == 1
        assert page.locator('.favorite-item').count() == 3
        page.screenshot(path=str(output/'reviewed-desktop.png'), full_page=True)
        page.get_by_label('个人决策筛选').select_option('undecided')
        page.get_by_role('button', name='应用', exact=True).click()
        page.wait_for_load_state('networkidle')
        assert page.locator('.favorite-item').count() == 2
        page.get_by_role('link', name='Agent Memory: A Research Starting Point', exact=True).click()
        page.wait_for_load_state('networkidle')
        panel = page.get_by_test_id('personal-decision')
        assert panel.is_visible()
        assert page.get_by_text('最新一次全文任务失败', exact=False).count() == 1
        print('Observed decision buttons:', panel.get_by_role('button').all_text_contents(), flush=True)
        panel.get_by_role('button', name='收藏', exact=True).click()
        page.wait_for_url('**/papers/1#fulltext-result')
        page.wait_for_load_state('networkidle')
        assert panel.get_by_role('button', name='已收藏', exact=True).get_attribute('aria-pressed') == 'true'
        assert panel.get_by_role('status').inner_text() == '已收藏此论文'
        assert page.locator('#fulltext-result h2').bounding_box()['y'] >= page.locator('.topbar').bounding_box()['height']
        page.locator('#fulltext-result').screenshot(path=str(output/'decision-desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        visit('/papers/1')
        visit('/papers/1#fulltext-result')
        assert page.locator('#fulltext-result h2').bounding_box()['y'] >= page.locator('.topbar').bounding_box()['height']
        panel.screenshot(path=str(output/'decision-mobile.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        visit('/favorites')
        assert page.locator('.favorite-item').count() == 1
        page.screenshot(path=str(output/'favorites-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        page.reload()
        page.wait_for_load_state('networkidle')
        assert page.locator('.favorite-item').count() == 1
        visit('/papers/1#fulltext-result')
        panel.get_by_role('button', name='跳过', exact=True).click()
        page.wait_for_load_state('networkidle')
        assert panel.get_by_role('button', name='已跳过', exact=True).get_attribute('aria-pressed') == 'true'
        visit('/favorites')
        assert page.locator('.favorite-item').count() == 0
        visit('/reviewed-papers?decision=skipped')
        assert page.locator('.favorite-item').count() == 2
        page.screenshot(path=str(output/'reviewed-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        visit('/papers/1#fulltext-result')
        panel.get_by_role('button', name='恢复未处理', exact=True).click()
        page.wait_for_load_state('networkidle')
        assert panel.get_by_text('未处理', exact=True).count() == 1
        visit('/reviewed-papers?decision=undecided')
        assert page.locator('.favorite-item').count() == 2
        visit('/papers/2')
        assert page.get_by_test_id('personal-decision').count() == 0
        assert not errors, errors
        browser.close()
    print('B first-stage browser checks passed:', output, flush=True)


if __name__ == '__main__':
    main()
