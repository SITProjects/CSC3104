// The filters in the My jobs column headers are <details> dropdowns: opening one
// closes the others, and a click outside or Escape closes them all.
const menus = [...document.querySelectorAll("details.header-filter")];
const closeAll = (except) => menus.forEach((d) => { if (d !== except) d.open = false; });
menus.forEach((d) => d.addEventListener("toggle", () => { if (d.open) closeAll(d); }));
document.addEventListener("click", (e) => closeAll(menus.find((d) => d.contains(e.target))));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAll(); });
