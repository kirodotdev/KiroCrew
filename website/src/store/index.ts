import { configureStore } from '@reduxjs/toolkit'
import { useDispatch, useSelector, useStore } from 'react-redux'
import dashboardReducer from './dashboardSlice'
import notificationsReducer, { addNotification } from './notificationsSlice'
import { installAppNoticeFeed } from '../lib/appNotice'
import chatReducer from './chatSlice'
import instancesReducer from './instancesSlice'

export const store = configureStore({
  reducer: {
    dashboard: dashboardReducer,
    notifications: notificationsReducer,
    chat: chatReducer,
    instances: instancesReducer,
  },
})

// App notices (lib/appNotice) record into the feed through this sink, so that
// module never imports the store and store-free surfaces can still use it.
installAppNoticeFeed(note => { store.dispatch(addNotification(note)) })

export type RootState = ReturnType<typeof store.getState>
export type AppDispatch = typeof store.dispatch
export type AppStore = typeof store
export const useAppDispatch = useDispatch.withTypes<AppDispatch>()
export const useAppSelector = useSelector.withTypes<RootState>()
export const useAppStore = useStore.withTypes<AppStore>()
