const form = document.querySelector('#dispatch-form');
const status = document.querySelector('#dispatch-status');
if (form) {
  const receipt = form.dataset.receipt;
  const panel = document.querySelector('#reevaluate-panel');

  async function call(path, payload) {
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || '操作失败');
    return result;
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const lots = [...form.querySelectorAll('input[name=lots]:checked')].map(input => input.value);
    const button = document.querySelector('#confirm-release');
    button.disabled = true;
    status.textContent = '留档中…';
    try {
      const result = await call(`/api/dispatch/${receipt}/confirm`, {lots});
      status.textContent = result.reused
        ? `放行单 ${result.order_id.slice(0, 8)} 已存在，未重复出库`
        : `已放行 ${result.lines.length} 批并出库留档`;
      location.reload();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });

  document.querySelector('#reevaluate-btn').addEventListener('click', () => {
    panel.hidden = !panel.hidden;
  });
  document.querySelector('#reevaluate-cancel').addEventListener('click', () => {
    panel.hidden = true;
  });

  document.querySelector('#reevaluate-submit').addEventListener('click', async () => {
    const sample = document.querySelector('#reevaluate-sample').value;
    const raw = document.querySelector('#reevaluate-pack').value.trim();
    let payload;
    if (sample) {
      payload = {sample};
    } else {
      try {
        payload = JSON.parse(raw);
      } catch (error) {
        status.textContent = '原件 JSON 无法解析';
        return;
      }
    }
    const button = document.querySelector('#reevaluate-submit');
    button.disabled = true;
    status.textContent = '重新判定中…';
    try {
      const result = await call(`/api/dispatch/${receipt}/reevaluate`, payload);
      status.textContent = (result.unchanged ? '原件未变，沿用当前归档；' : `已按新归档 ${result.current_id.slice(0, 8)} 重判；`)
        + `待追查：${result.flags.join('、') || '无'}`;
      location.reload();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
}
