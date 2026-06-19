const form = document.getElementById('loginForm')
const email = document.getElementById('loginEmail')
const password = document.getElementById('loginPassword')
const showPassword = document.getElementById('showPassword')
const errorMessage = document.getElementById('loginError')
const submitButton = document.getElementById('loginButton')

showPassword.addEventListener('change', () => {
  password.type = showPassword.checked ? 'text' : 'password'
})

form.addEventListener('submit', async (event) => {
  event.preventDefault()
  errorMessage.hidden = true
  submitButton.disabled = true
  submitButton.textContent = 'Comprobando...'
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.value.trim(), password: password.value }),
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || 'No se pudo iniciar sesion')
    window.location.replace('/')
  } catch (error) {
    errorMessage.textContent = error.message
    errorMessage.hidden = false
    password.select()
  } finally {
    submitButton.disabled = false
    submitButton.textContent = 'Entrar'
  }
})
