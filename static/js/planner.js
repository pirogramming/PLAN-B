document.addEventListener('DOMContentLoaded', function () {

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


  document.querySelectorAll('.js-filter').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.js-filter').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');

      const onlyLeft = btn.dataset.filter === 'left';
      document.querySelectorAll('.task').forEach(function (row) {
        row.hidden = onlyLeft && row.classList.contains('is-done');
      });
    });
  });


  document.querySelectorAll('.js-hide-notice').forEach(function (btn) {
    btn.addEventListener('click', function () {
      btn.closest('.banner').hidden = true;
    });
  });


/* ---------- 캘린더 : 날짜 선택 ---------- */
  const calDetail = document.getElementById('calDetail');
  if (calDetail) {
    const dateEl  = document.getElementById('calDetailDate');
    const sumEl   = document.getElementById('calDetailSum');
    const emptyEl = document.getElementById('calDetailEmpty');
    const panes   = calDetail.querySelectorAll('.cal-pane');
 
    document.querySelectorAll('.js-day').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const key = btn.dataset.date;
 
        document.querySelectorAll('.js-day').forEach(d => d.classList.remove('on'));
        btn.classList.add('on');
 
        let found = null;
        panes.forEach(function (pane) {
          const match = pane.dataset.date === key;
          pane.hidden = !match;
          if (match) found = pane;
        });
 
        const [, m, dd] = key.split('-').map(Number);
        dateEl.textContent = m + '월 ' + dd + '일';
        sumEl.textContent = '계획 ' + (btn.dataset.planned || 0) + '분 · 가능 ' + (btn.dataset.available || 0) + '분';
        emptyEl.hidden = !!found;
        if (!found) emptyEl.textContent = calDetail.dataset.emptyMsg;
 
        calDetail.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
    });
  }
 
  
  /* ---------- 복구안 선택 ---------- */
  const recLabel = document.getElementById('recLabel');
  const previewBtn = document.getElementById('previewBtn');
  document.querySelectorAll('.js-rec').forEach(function (input) {
    input.addEventListener('change', function () {
      document.querySelectorAll('.rec-card').forEach(function (card) {
        card.classList.toggle('on', card.contains(input) && input.checked);
      });
      if (recLabel) {
        recLabel.textContent = input.closest('.rec-card').querySelector('h3').textContent;
      }
      if (previewBtn) {
        previewBtn.dataset.planId = input.value;
      }
    });
  });


  const eod = document.getElementById('eodModal');
  if (eod) {
    const confirmView   = document.getElementById('eodConfirm');
    const progressView  = document.getElementById('eodProgress');
    const progressFoot  = document.getElementById('eodProgressFoot');
    const progressDone  = document.getElementById('eodProgressDone');
    const steps = eod.querySelectorAll('.steps-run li');
    let recalcDone = false;

    document.querySelectorAll('.js-end-day').forEach(function (btn) {
      btn.addEventListener('click', function () {
        confirmView.hidden = false;
        progressView.hidden = true;
        progressFoot.hidden = true;
        recalcDone = false;
        steps.forEach(s => s.classList.remove('doing', 'done'));
        eod.hidden = false;
        document.body.style.overflow = 'hidden';
      });
    });

    function closeEod() {
      if (!progressView.hidden && !recalcDone) return;
      eod.hidden = true;
      document.body.style.overflow = '';
    }
    eod.querySelectorAll('.js-close').forEach(b => b.addEventListener('click', closeEod));
    eod.addEventListener('click', e => { if (e.target === eod) closeEod(); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !eod.hidden) closeEod();
    });
    if (progressDone) progressDone.addEventListener('click', closeEod);

    const eodSubmit = document.getElementById('eodSubmit');
    if (eodSubmit) {
      eodSubmit.addEventListener('click', function () {
        confirmView.hidden = true;
        progressView.hidden = false;
        runSteps(0);
      });
    }

    function runSteps(i) {
      if (i > 0) {
        steps[i - 1].classList.remove('doing');
        steps[i - 1].classList.add('done');
      }
      if (i >= steps.length) {
        recalcDone = true;
        progressFoot.hidden = false;
        return;
      }
      steps[i].classList.add('doing');
      setTimeout(() => runSteps(i + 1), 500);
    }
  }


  const modal = document.getElementById('resultModal');
  if (!modal) return;

  const el = {
    tags:    document.getElementById('rmTags'),
    title:   document.getElementById('rmTitle'),
    meta:    document.getElementById('rmMeta'),
    time:    document.getElementById('rmTime'),
    hour:    document.getElementById('rmHour'),
    min:     document.getElementById('rmMin'),
    total:   document.getElementById('rmTotal'),
    timeHint:document.getElementById('rmTimeHint'),
    pctBox:  document.getElementById('rmPercent'),
    pct:     document.getElementById('rmPct'),
    pctHint: document.getElementById('rmPctHint'),
    none:    document.getElementById('rmNone'),
    submit:  document.getElementById('rmSubmit'),
  };

  let status = null;

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

  modal.querySelectorAll('.js-close').forEach(b => b.addEventListener('click', close));
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !modal.hidden) close(); });

  function close() {
    modal.hidden = true;
    document.body.style.overflow = '';
  }

  document.querySelectorAll('#rmPick .pick-item').forEach(function (item) {
    item.addEventListener('click', function () {
      status = item.dataset.value;
      paint();
    });
  });

  [el.hour, el.min, el.pct].forEach(i => i.addEventListener('input', paint));

  function paint() {
    document.querySelectorAll('#rmPick .pick-item').forEach(function (item) {
      item.classList.toggle('on', item.dataset.value === status);
    });

    el.time.hidden   = !(status === 'done' || status === 'partial');
    el.pctBox.hidden = status !== 'partial';
    el.none.hidden   = status !== 'not_done';

    const hourRaw = el.hour.value;
    const minRaw  = el.min.value;
    const h = Number(hourRaw);
    const m = Number(minRaw);

    const hourOk = hourRaw !== '' && Number.isInteger(h) && h >= 0 && h <= 23;
    const minOk  = minRaw  !== '' && Number.isInteger(m) && m >= 0 && m <= 59;
    const hourTyped = hourRaw !== '';
    const minTyped  = minRaw !== '';

    const minutes = (hourOk ? h : 0) * 60 + (minOk ? m : 0);
    const timeOk = hourOk && minOk && minutes > 0;

    el.total.textContent = minutes >= 60
      ? Math.floor(minutes / 60) + '시간 ' + (minutes % 60) + '분'
      : minutes + '분';
    el.hour.classList.toggle('zero', hourOk && h === 0);
    el.min.classList.toggle('zero', minOk && m === 0);
    el.hour.classList.toggle('err', hourTyped && !hourOk);
    el.min.classList.toggle('err', minTyped && !minOk);
    el.timeHint.classList.toggle('err', (hourTyped && !hourOk) || (minTyped && !minOk));

    const pct = parseInt(el.pct.value, 10);
    const pctOk = Number.isInteger(pct) && pct >= 1 && pct <= 99;
    const pctTyped = el.pct.value !== '';
    el.pct.classList.toggle('err', pctTyped && !pctOk);
    el.pctHint.classList.toggle('err', pctTyped && !pctOk);

    let ok = false;
    if (status === 'not_done')      ok = true;
    else if (status === 'done')     ok = timeOk;
    else if (status === 'partial')  ok = timeOk && pctOk;

    el.submit.disabled = !ok;
    el.submit.textContent =
      status === 'done'     ? '완료로 기록' :
      status === 'partial'  ? '일부완료로 기록' :
      status === 'not_done' ? '못함으로 기록' : '기록하기';
  }
});