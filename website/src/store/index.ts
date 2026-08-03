import { configureStore } from '@reduxjs/toolkit'
import { useDispatch, useSelector } from 'react-redux'
import dashboardReducer from './dashboardSlice'
import notificationsReducer from './notificationsSlice'
import chatReducer from './chatSlice'
import instancesReducer from './instancesSlice'
import appStatusReducer from './appStatusSlice'

export const store = configureStore({
  reducer: {
    dashboard: dashboardReducer,
    notifications: notificationsReducer,
    chat: chatReducer,
    instances: instancesReducer,
    appStatus: appStatusReducer,
  },
})

export type RootState = ReturnType<typeof store.getState>
export type AppDispatch = typeof store.dispatch
export const useAppDispatch = useDispatch.withTypes<AppDispatch>()
export const useAppSelector = useSelector.withTypes<RootState>()
