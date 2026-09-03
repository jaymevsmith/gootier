/* Jhome auth kit — shared behaviour for the split auth pages.
   Classic script (no modules): these are plain server-rendered
   templates, not part of any app bundle. Byte-identical per project. */

/* Show or clear a .jauth-alert. Empty text hides it again.
   textContent, never innerHTML — some messages carry an address the
   user just typed. */
function jauthAlert(el, text) {
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('is-shown', !!text);
}

/* Replace the card with a terminal "we're done here" message. */
function jauthCardMessage(heading, body) {
  var card = document.querySelector('.jauth-card');
  if (!card) return;
  card.textContent = '';
  var h = document.createElement('h1');
  h.textContent = heading;
  var p = document.createElement('p');
  p.className = 'jauth-sub';
  p.textContent = body;
  card.appendChild(h);
  card.appendChild(p);
}

document.addEventListener('DOMContentLoaded', function () {
  var toggles = document.querySelectorAll('.jauth-pw-toggle');
  for (var i = 0; i < toggles.length; i++) {
    toggles[i].addEventListener('click', function () {
      var input = document.getElementById(this.getAttribute('data-target'));
      if (!input) return;
      var reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      this.setAttribute('aria-pressed', reveal ? 'true' : 'false');
      this.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
      this.classList.toggle('is-revealed', reveal);
    });
  }
});
