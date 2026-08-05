// PLAN B — planner 화면 공통 스크립트

document.addEventListener('DOMContentLoaded', function () {

  /* ---------- 사이드바 접기 ---------- */
  const app = document.querySelector('.app');
  const toggleBtns = [document.getElementById('toggleSide'), document.getElementById('openSide')];

  if (app && sessionStorage.getItem('sideCollapsed') === '1') app.classList.add('collapsed');

  toggleBtns.forEach(function (btn) {
    if (!btn) return;
    btn.addEventListener('click', function () {
      app.classList.toggle('collapsed');
      sessionStorage.setItem('sideCollapsed', app.classList.contains('collapsed') ? '1' : '0');
    });
  });


  /* ---------- 작업 필터 ---------- */
  document.querySelectorAll('.js-filter').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.js-filter').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');

      const onlyLeft = btn.dataset.filter === 'left';
      document.querySelectorAll('.task').forEach(function (row) {
        // '안 끝난 것' = 완료를 제외한 나머지 (미입력 + 일부완료 + 못함)
        row.hidden = onlyLeft && row.classList.contains('is-done');
      });
    });
  });


  /* ---------- 결과 입력 모달 ---------- */
  const modal = document.getElementById('resultModal');
  if (!modal) return;

  const el = {
    tags:   document.getElementById('rmTags'),
    title:  document.getElementById('rmTitle'),
    meta:   document.getElementById('rmMeta'),
    time:   document.getElementById('rmTime'),
    hour:   document.getElementById('rmHour'),
    min:    document.getElementById('rmMin'),
    total:  document.getElementById('rmTotal'),
    pctBox: document.getElementById('rmPercent'),
    pct:    document.getElementById('rmPct'),
    pctHint:document.getElementById('rmPctHint'),
    none:   document.getElementById('rmNone'),
    submit: document.getElementById('rmSubmit'),
  };

  let status = null;   // done / partial / not_done

  /* 열기 */
  document.querySelectorAll('.js-result').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const row = btn.closest('.task');
      el.tags.innerHTML = row.querySelector('.task-tags').innerHTML;
      el.title.textContent = row.querySelector('.task-title').textContent;
      el.meta.textContent = row.querySelector('.task-time .l').textContent;

      status = btn.dataset.status || null;
      const prevMinutes = parseInt(btn.dataset.minutes, 10) || 0;
      el.hour.value = Math.floor(prevMinutes / 60);
      el.min.value = prevMinutes % 60;
      el.pct.value = btn.dataset.percent || '';
      modal.hidden = false;
      document.body.style.overflow = 'hidden';
      paint();
    });
  });

  /* 닫기 */
  modal.querySelectorAll('.js-close').forEach(b => b.addEventListener('click', close));
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !modal.hidden) close(); });

  function close() {
    modal.hidden = true;
    document.body.style.overflow = '';
  }

  /* 상태 선택 */
  document.querySelectorAll('#rmPick .pick-item').forEach(function (item) {
    item.addEventListener('click', function () {
      status = item.dataset.value;
      paint();
    });
  });

  /* 입력 감지 */
  [el.hour, el.min, el.pct].forEach(i => i.addEventListener('input', paint));

  /* 화면 갱신 + 버튼 활성화 검증 */
  function paint() {
    document.querySelectorAll('#rmPick .pick-item').forEach(function (item) {
      item.classList.toggle('on', item.dataset.value === status);
    });

    el.time.hidden   = !(status === 'done' || status === 'partial');
    el.pctBox.hidden = status !== 'partial';
    el.none.hidden   = status !== 'not_done';

    const h = parseInt(el.hour.value, 10) || 0;
    const m = parseInt(el.min.value, 10) || 0;
    const minutes = h * 60 + m;

    el.total.textContent = minutes >= 60
      ? Math.floor(minutes / 60) + '시간 ' + (minutes % 60) + '분'
      : minutes + '분';
    el.hour.classList.toggle('zero', h === 0);
    el.min.classList.toggle('zero', m === 0);

    const pct = parseInt(el.pct.value, 10);
    const pctOk = Number.isInteger(pct) && pct >= 1 && pct <= 99;
    const pctTyped = el.pct.value !== '';
    el.pct.classList.toggle('err', pctTyped && !pctOk);
    el.pctHint.classList.toggle('err', pctTyped && !pctOk);

    let ok = false;
    if (status === 'not_done')      ok = true;
    else if (status === 'done')     ok = minutes > 0;
    else if (status === 'partial')  ok = minutes > 0 && pctOk;

    el.submit.disabled = !ok;
    el.submit.textContent =
      status === 'done'     ? '완료로 기록' :
      status === 'partial'  ? '일부완료로 기록' :
      status === 'not_done' ? '못함으로 기록' : '기록하기';
  }
});