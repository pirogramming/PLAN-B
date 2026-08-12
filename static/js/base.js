document.addEventListener('DOMContentLoaded', function () {
  var messages = document.querySelectorAll('.site-message');
  if (!messages.length) return;

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
