import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'
import FleetApp from './fleet/FleetApp'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {window.location.pathname.startsWith('/fleet') ? <FleetApp /> : <App />}
  </StrictMode>,
)
