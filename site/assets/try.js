(() => {
  'use strict';
  const stage = document.getElementById('stage');
  const people = [{name:'David Smith',course:'Physics 212'},{name:'Anna Smith',course:'Math 241'}];
  function begin() {
    stage.innerHTML = '<p class="label">YOUR REQUEST</p><h2>“Reply to Professor Smith. Say I’ll be ten minutes late.”</h2><p>Two people fit that request. Which one did you mean?</p><div class="options"></div>';
    const options = stage.querySelector('.options');
    people.forEach(person => {
      const button = document.createElement('button');
      button.textContent = `${person.name} · ${person.course}`;
      button.onclick = () => preview(person);
      options.append(button);
    });
  }
  function preview(person) {
    stage.innerHTML = '<p class="label">MEANING RESOLVED · NOW REVIEW THE ACTION</p><h2>Send this reply?</h2><div class="result"><p id="recipient"></p><p>I’ll be ten minutes late.</p></div><div class="options"><button class="primary" id="yes">Yes — show the illustrated outcome</button><button id="change">Choose someone else</button></div>';
    document.getElementById('recipient').textContent = `To: Professor ${person.name} (${person.course})`;
    document.getElementById('change').onclick = begin;
    document.getElementById('yes').onclick = () => {
      stage.innerHTML = '<p class="label">ILLUSTRATION COMPLETE · NO EMAIL SENT</p><h2>Your choice changed the destination.</h2><p id="outcome"></p><p>“Which person?” established your intent. “Can I send?” asked for approval. AVO is designed to ask both.</p><p><a href="./#demo">Watch the real three-minute demo →</a></p>';
      document.getElementById('outcome').textContent = `The illustrated reply was addressed to ${person.name}, ${person.course}.`;
    };
  }
  document.getElementById('reset').onclick = begin;
  begin();
})();
