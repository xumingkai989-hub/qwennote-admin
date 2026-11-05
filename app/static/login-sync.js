(() => {
  let finished = false;
  const poll = async () => {
    if (finished) return;
    try {
      const response = await fetch('/api/sync/status', {credentials: 'same-origin', cache: 'no-store'});
      if (response.status === 401 || response.status === 403) return;
      if (response.ok) {
        const state = await response.json();
        if (state.running === false) {
          finished = true;
          window.location.reload();
          return;
        }
      }
    } catch (_) {
      // A temporary network failure is not evidence that sync has finished.
    }
    setTimeout(poll, 3000);
  };
  setTimeout(poll, 3000);
})();
