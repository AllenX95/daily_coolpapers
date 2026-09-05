(() => {
  const copy = document.getElementById('copy-diagnostic');
  if (copy) copy.addEventListener('click', async () => {
    const status = document.getElementById('copy-status');
    try {
      const response = await fetch(copy.dataset.url, {cache: 'no-store'});
      if (!response.ok) throw new Error('request failed');
      const text = await response.text();
      await navigator.clipboard.writeText(text);
      status.textContent = '已复制当前筛选的本页诊断（非全部事件）。';
    } catch (_) {
      status.textContent = '复制不可用，请打开右侧诊断 JSON 手动复制。';
    }
  });
  const record = document.querySelector('.run-record');
  if (record && record.dataset.active === '1') setInterval(() => {
    if (!document.hidden && !document.querySelector('form:focus-within')) location.reload();
  }, 5000);
})();
