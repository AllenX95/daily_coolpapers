"""Optional Playwright UI checks; not a dependency of the unittest suite."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:18765')
    parser.add_argument('--output', default='tmp/a4-browser-results')
    parser.add_argument('--channel', default='msedge')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=args.channel)
        context = browser.new_context(viewport={'width': 1440, 'height': 1100}, permissions=['clipboard-read', 'clipboard-write'])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url+'/jobs/1')
        page.wait_for_load_state('networkidle')
        print('Observed buttons:', page.get_by_role('button').all_text_contents(), flush=True)
        page.screenshot(path=str(output/'detail-desktop.png'), full_page=True)
        assert page.get_by_text('network_timeout', exact=True).count() == 2
        page.get_by_role('link', name='完整时间线', exact=True).click()
        page.wait_for_load_state('networkidle')
        assert page.get_by_text('论文评估成功', exact=True).count() == 1
        page.get_by_role('button', name='复制本页安全诊断').click()
        page.get_by_role('status').filter(has_text='已复制当前筛选').wait_for()
        diagnostic = json.loads(page.evaluate('navigator.clipboard.readText()'))
        assert diagnostic['job_id'] == 1
        assert diagnostic['status'] == 'partial_success'
        page.locator('select[name="severity"]').select_option('warning')
        page.locator('select[name="category"]').select_option('cs.AI')
        page.get_by_role('button', name='筛选记录').click()
        page.wait_for_load_state('networkidle')
        assert page.get_by_text('当前筛选没有事件。', exact=False).count() == 1
        page.goto(args.url+'/jobs/1')
        page.wait_for_load_state('networkidle')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(output/'detail-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        table = page.get_by_role('region', name='阶段事件记录（可横向滚动）')
        table.evaluate('(element) => { element.scrollLeft = element.scrollWidth; }')
        assert table.evaluate('(element) => element.scrollLeft > 0')
        page.screenshot(path=str(output/'detail-mobile-events.png'), full_page=True)
        page.set_viewport_size({'width': 1440, 'height': 1100})
        page.goto(args.url)
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(output/'home-desktop.png'), full_page=True)
        assert page.get_by_text('部分完成', exact=True).count() >= 1
        page.goto(args.url+'/jobs/1')
        page.wait_for_load_state('networkidle')
        page.get_by_role('button', name='只补评缺失摘要', exact=True).click()
        page.wait_for_url('**/jobs/2')
        assert page.get_by_text('排队中', exact=True).count() >= 1
        assert not errors, errors
        browser.close()
    print('UI checks passed:', output, flush=True)


if __name__ == '__main__':
    main()
