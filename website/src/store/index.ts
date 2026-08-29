import { configureStore } from '@reduxjs/toolkit'
import { useDispatch, useSelector, useStore } from 'react-redux'
import dashboardReducer from './dashboardSlice'
import notificationsReducer from './notificationsSlice'
import chatReducer from './chatSlice'
import instancesReducer from './instancesSlice'
import { slotsResidueListener } from './slotsResidueListener'

export const store = configureStore({
  reducer: {
    dashboard: dashboardReducer,
    notifications: notificationsReducer,
    chat: chatReducer,
    instances: instancesReducer,
  },
  middleware: getDefault => getDefault().prepend(slotsResidueListener.middleware),
})

export type RootState = ReturnType<typeof store.getState>
export type AppDispatch = typeof store.dispatch
export type AppStore = typeof store
export const useAppDispatch = useDispatch.withTypes<AppDispatch>()
export const useAppSelector = useSelector.withTypes<RootState>()
export const useAppStore = useStore.withTypes<AppStore>()
