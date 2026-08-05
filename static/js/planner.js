document.addEventListener('DOMContentLoaded', function () {
  const app = document.querySelector('.app');
  const btn = document.getElementById('toggleSide');
  const open = document.getElementById('openSide');

  if (sessionStorage.getItem('sideCollapsed') === '1') app.classList.add('collapsed');

  function toggle() {
    app.classList.toggle('collapsed');
    sessionStorage.setItem('sideCollapsed', app.classList.contains('collapsed') ? '1' : '0');
  }

  if (btn) btn.addEventListener('click', toggle);
  if (open) open.addEventListener('click', toggle);
});