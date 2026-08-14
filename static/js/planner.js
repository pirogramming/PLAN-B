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

    function selectDay(btn) {
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
    }

    document.querySelectorAll('.js-day').forEach(function (btn) {
      btn.addEventListener('click', function () {
        selectDay(btn);
        calDetail.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
    });

    // 오늘 날짜 칸은 이미 시각적으로 "오늘"로 표시돼 있어서, 페이지를 열자마자
    // 그 날짜가 이미 선택된 것처럼 보인다 — 아래 상세 패널도 처음부터 오늘
    // 학습 작업을 보여줘야 "날짜를 선택해주세요"라는 안내가 어색해지지 않는다.
    // (scrollIntoView는 안 부른다 — 페이지 진입하자마자 아래로 스크롤되면 안 됨)
    const todayBtn = document.querySelector('.js-day.today');
    if (todayBtn) {
      selectDay(todayBtn);
    }
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
        previewBtn.href = input.dataset.previewUrl;
      }
    });
  });


  const eod = document.getElementById('eodModal');
  if (eod) {
    const confirmView   = document.getElementById('eodConfirm');
    const progressView  = document.getElementById('eodProgress');
    const progressHint  = document.getElementById('eodProgressHint');
    const progressFoot  = document.getElementById('eodProgressFoot');
    const availTimeBtn  = document.getElementById('eodAvailTimeBtn');
    const stepIco       = document.getElementById('eodStepIco');
    const stepLabel     = document.getElementById('eodStepLabel');
    const steps = eod.querySelectorAll('.steps-run li');
    let recalcDone = false;
    let finalizeResult = null;

    document.querySelectorAll('.js-end-day').forEach(function (btn) {
      btn.addEventListener('click', function () {
        confirmView.hidden = false;
        progressView.hidden = true;
        progressFoot.hidden = true;
        availTimeBtn.hidden = true;
        stepIco.innerHTML = '<use href="#i-loader"/>';
        stepLabel.textContent = '오늘 기록을 확정하는 중';
        progressHint.textContent = '잠시만 기다려 주세요.';
        recalcDone = false;
        finalizeResult = null;
        steps.forEach(s => s.classList.remove('doing', 'done', 'error'));
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

    function goToNextScreen() {
      if (finalizeResult.needs_recovery && finalizeResult.recovery_group_id) {
        window.location.href = eod.dataset.recoveryUrlTemplate.replace(
          '00000000-0000-0000-0000-000000000000', finalizeResult.recovery_group_id
        );
      } else {
        window.location.href = eod.dataset.todayUrl;
      }
    }

    const eodSubmit = document.getElementById('eodSubmit');
    if (eodSubmit) {
      eodSubmit.addEventListener('click', function () {
        confirmView.hidden = true;
        progressView.hidden = false;
        steps.forEach(s => s.classList.add('doing'));
        submitFinalize();
      });
    }

    function submitFinalize() {
      const csrfInput = eod.querySelector('[name=csrfmiddlewaretoken]');
      fetch(eod.dataset.finalizeUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfInput ? csrfInput.value : '' },
      })
        .then(function (res) {
          if (!res.ok) throw new Error('finalize failed: ' + res.status);
          return res.json();
        })
        .then(function (data) {
          finalizeResult = data;
          recalcDone = true;

          // needs_recovery=true인데 recovery_group_id가 없으면(=recovery_available
          // false) 지금 가용시간으로는 복구안 자체를 만들 수 없다는 뜻이다. 이때
          // goToNextScreen()의 두 분기(복구 화면 / 오늘 화면) 중 어디에도 안
          // 걸려서 조용히 오늘 화면으로 돌아가버리던 게 원래 버그 — 여기서
          // 따로 잡아서 안내하고, 자동 이동도 하지 않는다 (사용자가 가용시간을
          // 고치기 전까진 오늘 화면으로 가봤자 똑같은 상황이 반복되므로).
          if (finalizeResult.needs_recovery && !finalizeResult.recovery_group_id) {
            steps.forEach(function (s) {
              s.classList.remove('doing');
              s.classList.add('error');
            });
            stepIco.innerHTML = '<use href="#i-alert"/>';
            stepLabel.textContent = '지금은 복구안을 만들 수 없어요';
            progressHint.textContent =
              '현재 가용시간으로 남은 작업을 재배치할 수 없습니다. 가용시간을 늘려주세요.';
            progressFoot.hidden = false;
            availTimeBtn.hidden = false;
            return;
          }

          steps.forEach(function (s) {
            s.classList.remove('doing');
            s.classList.add('done');
          });
          stepIco.innerHTML = '<use href="#i-done"/>';
          stepLabel.textContent = '오늘 기록이 확정됐습니다';
          progressHint.textContent = '잠시 후 자동으로 이동합니다.';
          // 완료 상태를 눈으로 확인할 시간을 잠깐 준 다음 자동으로 다음 화면으로
          // 이동한다 — "확인" 버튼을 눌러야 하는지 애매했던 이전 UX를 없앤다.
          setTimeout(goToNextScreen, 700);
        })
        .catch(function () {
          finalizeResult = { error: true };
          recalcDone = true;
          // 실패 시에는 done 처리하지 않는다 (마감이 안 됐는데 단계가 끝난 것처럼
          // 보이면 안 됨). 자동 이동도 하지 않고, 닫기 버튼만 보여준다.
          steps.forEach(function (s) {
            s.classList.remove('doing', 'done');
            s.classList.add('error');
          });
          stepIco.innerHTML = '<use href="#i-alert"/>';
          stepLabel.textContent = '확정에 실패했습니다';
          progressHint.textContent = '오류가 발생했습니다. 잠시 후 다시 시도해 주세요.';
          progressFoot.hidden = false;
        });
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
  let currentItemId = null;

  document.querySelectorAll('.js-result').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const row = btn.closest('.task');
      el.tags.innerHTML = row.querySelector('.task-tags').innerHTML;
      el.title.textContent = row.querySelector('.task-title').textContent;
      el.meta.textContent = row.querySelector('.task-time .l').textContent;

      currentItemId = btn.dataset.id;
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

  el.submit.addEventListener('click', function () {
    if (el.submit.disabled || !currentItemId) return;

    const hour = Number(el.hour.value) || 0;
    const min  = Number(el.min.value) || 0;

    const payload = { status: status };
    payload.actual_minutes = (status === 'done' || status === 'partial')
      ? hour * 60 + min
      : 0;
    if (status === 'partial') {
      payload.completion_percent = parseInt(el.pct.value, 10);
    }

    const csrfInput = modal.querySelector('[name=csrfmiddlewaretoken]');
    const url = modal.dataset.progressUrlTemplate.replace('999999999', currentItemId);

    el.submit.disabled = true;
    el.submit.textContent = '저장하는 중...';

    fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRFToken': csrfInput ? csrfInput.value : '',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    })
      .then(function (res) {
        if (!res.ok) throw new Error('progress record failed: ' + res.status);
        window.location.reload();
      })
      .catch(function () {
        el.submit.textContent = '실패했어요, 다시 시도';
        setTimeout(paint, 2000);
      });
  });
});