// Progressive enhancement: without JavaScript both field groups remain usable.
document.querySelectorAll('[data-team-entity]').forEach(entity => {
  const sync = () => {
    const mode = entity.querySelector('input[type="radio"]:checked')?.value;
    entity.querySelectorAll('[data-mode-fields]').forEach(group => {
      const active = group.dataset.modeFields === mode;
      group.hidden = !active;
      group.querySelectorAll('input, select, textarea').forEach(control => {
        control.disabled = !active;
      });
    });
  };
  entity.querySelectorAll('input[type="radio"]').forEach(radio => radio.addEventListener('change', sync));
  sync();
});
