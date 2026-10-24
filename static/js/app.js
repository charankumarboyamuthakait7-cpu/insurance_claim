/* Progressive enhancement only. The server validates and authorises every request. */
(function () {
  "use strict";

  /** Toggle the compact navigation drawer on smaller screens. */
  var sidebarToggle = document.getElementById("sidebarToggle");
  var sidebar = document.getElementById("appSidebar");
  if (sidebarToggle && sidebar) {
    sidebarToggle.addEventListener("click", function () {
      sidebar.classList.toggle("is-open");
    });
    document.addEventListener("click", function (event) {
      if (window.innerWidth <= 760 && sidebar.classList.contains("is-open") &&
          !sidebar.contains(event.target) && !sidebarToggle.contains(event.target)) {
        sidebar.classList.remove("is-open");
      }
    });
  }

  /** Submit a GET form as soon as an auto-submit select changes (dependent member loading). */
  document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () {
      if (el.value) { el.form.submit(); }
    });
  });

  /** Ask for confirmation before irreversible workflow buttons. */
  document.querySelectorAll("[data-confirm]").forEach(function (btn) {
    btn.addEventListener("click", function (event) {
      if (!window.confirm(btn.getAttribute("data-confirm"))) { event.preventDefault(); }
    });
  });

  /** Show Bootstrap inline validation on forms marked needs-validation. */
  document.querySelectorAll("form.needs-validation").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!form.checkValidity()) {
        event.preventDefault();
        event.stopPropagation();
      }
      form.classList.add("was-validated");
    });
  });

  /** Keep the discharge date from being earlier than the admission date. */
  var admission = document.getElementById("admission_date");
  var discharge = document.getElementById("discharge_date");
  if (admission && discharge) {
    var sync = function () { discharge.min = admission.value || ""; };
    admission.addEventListener("change", sync);
    sync();
  }
})();
