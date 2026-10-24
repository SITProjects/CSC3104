// Show/hide password toggles and a password-match check for the signup form.
document.querySelectorAll(".toggle-password").forEach((btn) => {
  btn.addEventListener("click", () => {
    const input = btn.parentElement.querySelector("input");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "Hide" : "Show";
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
  });
});

const signup = document.querySelector('form[action="/signup"]');
if (signup) {
  const pw = signup.elements.password;
  const confirm = signup.elements.confirm_password;
  const msg = document.getElementById("match-error");
  const check = () => {
    const mismatch = confirm.value !== "" && pw.value !== confirm.value;
    msg.hidden = !mismatch;
    return !mismatch;
  };
  pw.addEventListener("input", check);
  confirm.addEventListener("input", check);
  signup.addEventListener("submit", (e) => {
    if (!signup.checkValidity() || !check()) {
      e.preventDefault();
      signup.reportValidity();
    }
  });
}
