document.addEventListener('DOMContentLoaded', function () {
  var messages = document.querySelectorAll('.site-message');
  if (!messages.length) return;

  // 사이드바(.app .side)가 있는 화면에서는 토스트를 화면 전체 중앙이 아니라
  // 사이드바를 뺀 메인 영역 중앙에 띄운다. 사이드바 접기/펼치기·창 크기 변화로
  // 폭이 바뀌어도 ResizeObserver가 다시 계산해준다.
  var siteMessages = document.getElementById('siteMessages');
  var side = document.querySelector('.app .side');
  if (siteMessages && side) {
    var recenter = function () {
      var sideWidth = side.getBoundingClientRect().width;
      siteMessages.style.left = 'calc(50% + ' + (sideWidth / 2) + 'px)';
    };
    recenter();
    if (window.ResizeObserver) {
      new ResizeObserver(recenter).observe(side);
    } else {
      window.addEventListener('resize', recenter);
    }
  }

  var VISIBLE_MS = 4000;
  var FADE_MS = 250; // static/css/base.css의 .site-message transition 시간과 맞춰야 함

  setTimeout(function () {
    messages.forEach(function (el) {
      el.classList.add('is-leaving');
    });
    setTimeout(function () {
      messages.forEach(function (el) {
        el.remove();
      });
    }, FADE_MS);
  }, VISIBLE_MS);
});
