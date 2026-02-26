document.addEventListener('DOMContentLoaded', function () {
    // ── LIFF polyfill: stub all LIFF methods to prevent errors ──
    if (typeof liff === 'undefined') {
        window.liff = {
            isInClient: () => false,
            isLoggedIn: () => false,
            getProfile: () => Promise.resolve({ userId: '', displayName: '', pictureUrl: '' }),
            sendMessages: () => Promise.resolve(),
            shareTargetPicker: () => Promise.resolve(),
            login: () => { },
            logout: () => { },
            init: () => Promise.resolve(),
            openWindow: () => { }
        };
    }
    /**
     * 照片瀏覽器主要JavaScript代碼
     * 功能包括：
     * 1. 相簿瀏覽和無限滾動載入
     * 2. 照片延遲載入(Lazy Loading)
     * 3. 全螢幕照片查看
     * 4. LINE LIFF 整合（分享和傳送照片）
     * 5. 響應式設計處理
     * 6. 照片收藏功能
     */

    // 添加直式影片的樣式
    const portraitVideoStyle = document.createElement('style');
    portraitVideoStyle.textContent = `
        /* 直式影片在全螢幕模式下的樣式 */
        .portrait-video-container .plyr__video-wrapper {
            height: 100% !important;
        }
        
        .portrait-video-container video {
            object-fit: cover !important;
            height: 100% !important;
            width: 100% !important;
        }
        
        /* 確保播放器控制項不被影片覆蓋 */
        .portrait-video-container .plyr__controls {
            z-index: 10;
            position: relative;
        }
        
        /* 全螢幕時的影片容器樣式 */
        .swiper-slide .video-container-placeholder {
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100%;
            width: 100%;
        }
        
        /* 確保全螢幕時影片播放器容器高度 */
        .swiper-slide .plyr {
            height: 100%;
            width: 100%;
        }
    `;
    document.head.appendChild(portraitVideoStyle);

    /**
     * 預載入 Logo 圖片
     */
    function preloadLogo() {
        return new Promise((resolve, reject) => {
            const logoImg = document.querySelector('.preloader-logo');
            if (!logoImg) {
                resolve();
                return;
            }

            if (logoImg.complete && logoImg.naturalHeight !== 0) {
                // 圖片已經載入完成
                logoImg.classList.add('loaded');
                resolve();
            } else {
                // 等待圖片載入
                logoImg.onload = () => {
                    logoImg.classList.add('loaded');
                    resolve();
                };
                logoImg.onerror = () => {
                    console.warn('Logo 載入失敗，使用預設載入動畫');
                    resolve(); // 即使載入失敗也繼續執行
                };
            }
        });
    }

    /**
     * 顯示應用程式內容，隱藏預載入動畫
     */
    function showApp() {
        const preloader = document.getElementById('preloader');
        const appWrapper = document.getElementById('app-wrapper');

        if (preloader) {
            preloader.style.opacity = '0';
            preloader.style.visibility = 'hidden';
            preloader.addEventListener('transitionend', () => {
                preloader.style.display = 'none';
            });
        }

        if (appWrapper) {
            appWrapper.style.visibility = 'visible';
            appWrapper.style.opacity = '1';
        }
    }

    // ── Teacher Web App: skip LIFF, get folder_id from URL param ──
    function initTeacherAlbum() {
        const url = new URL(window.location.href);
        const folderId = url.searchParams.get('folder_id');
        if (!folderId) {
            document.getElementById('albums-container').innerHTML =
                '<div style="text-align:center;padding:60px 20px;color:#999;">請提供 folder_id 參數</div>';
            showApp();
            return;
        }
        window.folderIdList = [folderId];

        // Show UI elements directly (no login needed)
        document.getElementById('albums-container').style.display = 'block';
        document.querySelector('.year-toolbar').style.display = 'block';
        document.querySelector('.date-bar').style.display = 'flex';

        loadAlbums();
        showApp();
    }

    // 全域變數設定
    let swiper;                     // Swiper實例，用於全螢幕照片瀏覽
    let currentPage = 1;            // 目前載入的頁碼
    const itemsPerPage = 20;        // 每頁顯示的相簿數量
    let allFolders = [];            // 儲存所有相簿資料
    let isLoading = false;          // 防止重複載入的標記
    let allPhotosLoaded = false;    // 標記是否已載入所有照片
    let currentMonthIndex = 0;
    let userProfile = null;         // 儲存使用者資料
    let favoritePhotos = new Set(); // 儲存收藏的照片ID

    // 全局播放器實例
    let globalVideoPlayer = null;   // 全局共享的Plyr播放器實例
    let currentVideoId = null;      // 當前播放的影片ID

    // Start the app after all variables are declared
    initTeacherAlbum();

    // 影片顯示相關
    const isMobileDevice = window.matchMedia("(max-width: 768px)").matches; // 檢查是否為手機

    /**
     * 設置直式影片的樣式，讓影片內容放大填滿播放器
     * @param {Object} player - Plyr播放器實例
     */
    function handlePortraitVideo(player) {
        if (!player || !player.elements || !player.elements.original) return;

        const videoElement = player.elements.original;

        // 等待影片元數據加載完成
        if (videoElement.videoWidth === 0 || videoElement.videoHeight === 0) {
            videoElement.addEventListener('loadedmetadata', () => handlePortraitVideo(player), { once: true });
            return;
        }

        // 檢測影片方向
        const isPortrait = videoElement.videoHeight > videoElement.videoWidth;
        console.log(`影片方向檢測: ${isPortrait ? '直式' : '橫式'}, 尺寸: ${videoElement.videoWidth}x${videoElement.videoHeight}`);

        if (isPortrait && isMobileDevice) {
            console.log('應用直式影片樣式');

            // 設置影片元素樣式
            videoElement.style.objectFit = 'cover'; // 關鍵屬性：覆蓋填滿，必要時裁切
            videoElement.style.width = '100%';
            videoElement.style.height = '100%';

            // 添加標記類別，方便後續樣式調整
            const container = player.elements.container;
            if (container) {
                container.classList.add('portrait-video-container');
            }

            // 設置Plyr內部包裝器樣式
            const wrapper = player.elements.wrapper;
            if (wrapper) {
                wrapper.style.width = '100%';
                wrapper.style.height = '100%';
            }
        } else {
            // 橫式影片或非手機，使用預設樣式
            videoElement.style.objectFit = '';
            videoElement.style.width = '';
            videoElement.style.height = '';

            const container = player.elements.container;
            if (container) {
                container.classList.remove('portrait-video-container');
            }
        }
    }

    // 監聽屏幕方向變化，更新直式影片樣式
    window.addEventListener('orientationchange', () => {
        // 延遲執行，確保方向變化已完成
        setTimeout(() => {
            if (globalVideoPlayer) {
                handlePortraitVideo(globalVideoPlayer);
            }
        }, 300);
    });

    // 全螢幕滑動關閉相關變數
    let fsTouchStartY = 0;
    let fsIsDragging = false;
    let fsSwipeDirection = null;
    let fsTouchStartX = 0;

    // 收藏照片功能
    async function loadFavoritePhotos() {
        if (!userProfile || !userProfile.userId) return;

        try {
            const response = await fetch('https://student.wentzao.com/get_favorite_photos', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ userId: userProfile.userId })
            });
            const data = await response.json();
            if (data.status === 'success' && data.data && data.data.favorite_album) {
                favoritePhotos = new Set(data.data.favorite_album);
            }
        } catch (error) {
            console.error('無法載入收藏的照片:', error);
        }
    }

    async function toggleFavoriteStatus(photo) {
        if (!userProfile || !photo) return;

        const photoId = photo.photoId;
        const isFavorited = favoritePhotos.has(photoId);

        if (isFavorited) {
            favoritePhotos.delete(photoId);
        } else {
            favoritePhotos.add(photoId);
        }

        updateFavoriteIcons(photoId, !isFavorited);

        try {
            await fetch('https://student.wentzao.com/toggle_favorite_photo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    userId: userProfile.userId,
                    photoId: photoId
                })
            });
        } catch (error) {
            console.error('更新收藏狀態失敗:', error);
            // Revert local change on error
            if (isFavorited) {
                favoritePhotos.add(photoId);
            } else {
                favoritePhotos.delete(photoId);
            }
            updateFavoriteIcons(photoId, isFavorited);
            showNotification('操作失敗，請稍後再試');
        }
    }

    function updateFavoriteIcons(photoId, isFavorited) {
        // 更新網格視圖中的圖示
        const gridImg = document.querySelector(`.photo-item img[data-photoId="${photoId}"]`);
        if (gridImg) {
            const photoItem = gridImg.closest('.photo-item');
            if (isFavorited) {
                photoItem.classList.add('favorited');
            } else {
                photoItem.classList.remove('favorited');
            }
        }

        // 更新全螢幕視圖中的按鈕
        const fullscreenOverlay = document.querySelector('.fullscreen-overlay');
        if (fullscreenOverlay.style.display === 'block') {
            const favoriteButton = document.querySelector('.favorite-button');
            if (favoriteButton) {
                if (isFavorited) {
                    favoriteButton.classList.add('active');
                } else {
                    favoriteButton.classList.remove('active');
                }
            }
        }
    }

    // 添加電子賀卡相關的變數
    let isSelectingPhotos = false;
    let selectedPhotos = new Map();
    const cardInstructions = "從孩子的個人相簿選擇1-10張您最喜歡的照片，" +
        "我們將為您製作一張精美的電子賀卡。\n" +
        "您可以將這張賀卡分享給親朋好友，與他們一起分享孩子成長的喜悅。";

    // 初始化電子賀卡相關元素
    const christmasBtn = document.querySelector('.christmas-card-btn');
    const cardModal = document.querySelector('.card-modal');
    const understandBtn = document.querySelector('.understand-btn');
    const cardToolbar = document.querySelector('.card-toolbar');
    const cardStatusBtn = document.querySelector('.card-status-btn');
    const closeCardBtn = document.querySelector('.close-card-btn');

    // --- BEGIN: Swipe navigation for albums ---
    const albumContainer = document.getElementById('albums-container');
    let touchStartX = 0;
    let touchCurrentX = 0;
    let touchDeltaX = 0;
    let isSwiping = false;
    // --- BEGIN: Swipe direction locking ---
    let touchStartY = 0;
    let touchDeltaY = 0;
    let swipeDirection = null; // 'horizontal' or 'vertical'
    // --- END: Swipe direction locking ---

    // Only enable on mobile for a better experience
    if (window.matchMedia("(max-width: 768px)").matches) {
        albumContainer.addEventListener('touchstart', handleTouchStart, { passive: false });
        albumContainer.addEventListener('touchmove', handleTouchMove, { passive: false });
        albumContainer.addEventListener('touchend', handleTouchEnd, { passive: true });
    }

    function handleTouchStart(e) {
        if (e.touches.length > 1) return;
        isSwiping = true;
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        touchDeltaX = 0;
        touchDeltaY = 0;
        swipeDirection = null; // Reset direction on new touch
        albumContainer.style.transition = 'none';
    }

    function handleTouchMove(e) {
        if (!isSwiping || e.touches.length > 1) return;
        touchCurrentX = e.touches[0].clientX;
        touchDeltaX = touchCurrentX - touchStartX;
        touchDeltaY = e.touches[0].clientY - touchStartY;

        // --- BEGIN: Determine swipe direction ---
        if (!swipeDirection) {
            const absDeltaX = Math.abs(touchDeltaX);
            const absDeltaY = Math.abs(touchDeltaY);

            // Wait for a minimum movement before deciding
            if (absDeltaX > 5 || absDeltaY > 5) {
                if (absDeltaX > absDeltaY) {
                    swipeDirection = 'horizontal';
                } else {
                    swipeDirection = 'vertical';
                }
            }
        }
        // --- END: Determine swipe direction ---

        if (swipeDirection === 'horizontal') {
            e.preventDefault(); // Prevent vertical scroll

            const isFavoritesView = document.body.classList.contains('favorites-view-active');
            const isLast = currentMonthIndex === allFolders.length - 1;

            // Add resistance at the edges for a "jelly" feel
            if ((isFavoritesView && touchDeltaX > 0) || (!isFavoritesView && isLast && touchDeltaX < 0)) {
                const resistance = 0.4;
                albumContainer.style.transform = `translateX(${touchDeltaX * resistance}px)`;
            } else {
                albumContainer.style.transform = `translateX(${touchDeltaX}px)`;
            }
        }
    }

    function handleTouchEnd(e) {
        if (!isSwiping || swipeDirection !== 'horizontal') {
            isSwiping = false;
            swipeDirection = null;
            return;
        }
        isSwiping = false;
        const swipeThreshold = window.innerWidth / 3.5; // Swipe distance needed to trigger change

        const isFavoritesView = document.body.classList.contains('favorites-view-active');
        const isFirst = currentMonthIndex === 0;
        const isLast = currentMonthIndex === allFolders.length - 1;

        // Use a bouncy spring-like transition for the return
        const bounceBackTransition = 'transform 0.4s cubic-bezier(0.2, 0.8, 0.2, 1)';

        if (Math.abs(touchDeltaX) > swipeThreshold) {
            if (isFavoritesView) {
                if (touchDeltaX < 0) { // Swipe left from favorites
                    changeFromFavoritesWithAnimation();
                } else { // Bounce back when swiping right
                    albumContainer.style.transition = bounceBackTransition;
                    albumContainer.style.transform = 'translateX(0)';
                }
            } else { // Not in favorites view
                if (touchDeltaX > 0) { // Swipe Right
                    if (isFirst) {
                        changeToFavoritesWithAnimation();
                    } else {
                        changeMonthWithAnimation('prev');
                    }
                } else if (touchDeltaX < 0) { // Swipe Left
                    if (!isLast) {
                        changeMonthWithAnimation('next');
                    } else { // Bounce back at the end
                        albumContainer.style.transition = bounceBackTransition;
                        albumContainer.style.transform = 'translateX(0)';
                    }
                }
            }
        } else {
            // Bounce back if swipe distance is not enough
            albumContainer.style.transition = bounceBackTransition;
            albumContainer.style.transform = 'translateX(0)';
        }

        touchDeltaX = 0;
        swipeDirection = null; // Reset direction
    }

    function changeToFavoritesWithAnimation() {
        const exitX = '100%';
        const entryX = '-100%';

        albumContainer.style.transition = 'transform 0.25s cubic-bezier(0.4, 0, 0.6, 1), opacity 0.25s ease';
        albumContainer.style.transform = `translateX(${exitX})`;
        albumContainer.style.opacity = '0';

        albumContainer.addEventListener('transitionend', function handler() {
            displayFavorites();

            window.scrollTo({
                top: 0,
                behavior: 'instant'
            });

            albumContainer.style.transition = 'none';
            albumContainer.style.transform = `translateX(${entryX})`;
            albumContainer.style.opacity = '1';

            void albumContainer.offsetHeight;

            albumContainer.style.transition = 'transform 0.4s cubic-bezier(0.2, 0.8, 0.2, 1)';
            albumContainer.style.transform = 'translateX(0)';

            document.querySelectorAll('.year-btn').forEach(btn => btn.classList.remove('active'));
            const favBtn = document.querySelector('.year-btn[data-year="favorites"]');
            if (favBtn) favBtn.classList.add('active');

            // 恢復滾動條
            albumContainer.addEventListener('transitionend', function endAnimation() {
                document.body.style.overflowX = '';
                albumContainer.removeEventListener('transitionend', endAnimation);
            }, { once: true });

        }, { once: true });
    }

    function changeFromFavoritesWithAnimation() {
        const exitX = '-100%';
        const entryX = '100%';

        albumContainer.style.transition = 'transform 0.25s cubic-bezier(0.4, 0, 0.6, 1), opacity 0.25s ease';
        albumContainer.style.transform = `translateX(${exitX})`;
        albumContainer.style.opacity = '0';

        albumContainer.addEventListener('transitionend', function handler() {
            document.body.classList.remove('favorites-view-active');
            currentMonthIndex = 0;

            // Repopulate album
            albumContainer.innerHTML = '';
            const newAlbumContent = createAlbumElement(allFolders[currentMonthIndex]);
            albumContainer.appendChild(newAlbumContent);
            lazyLoadImages();
            if (isSelectingPhotos) {
                updatePhotoSelectUI();
            }

            updateDateDisplay();

            // Update year toolbar button
            document.querySelectorAll('.year-btn').forEach(btn => btn.classList.remove('active'));
            const latestBtn = document.querySelector('.year-btn[data-year="all"]');
            if (latestBtn) latestBtn.classList.add('active');

            // Scroll to top
            window.scrollTo({ top: 0, behavior: 'instant' });

            // Position for entry animation
            albumContainer.style.transition = 'none';
            albumContainer.style.transform = `translateX(${entryX})`;
            albumContainer.style.opacity = '1';

            void albumContainer.offsetHeight;

            // Animate in
            albumContainer.style.transition = 'transform 0.4s cubic-bezier(0.2, 0.8, 0.2, 1)';
            albumContainer.style.transform = 'translateX(0)';

            // 恢復滾動條
            albumContainer.addEventListener('transitionend', function endAnimation() {
                document.body.style.overflowX = '';
                albumContainer.removeEventListener('transitionend', endAnimation);
            }, { once: true });

        }, { once: true });
    }

    function changeMonthWithAnimation(direction) {
        const exitX = direction === 'next' ? '-100%' : '100%';
        const entryX = direction === 'next' ? '100%' : '-100%';

        // Animate out the current album
        albumContainer.style.transition = 'transform 0.25s cubic-bezier(0.4, 0, 0.6, 1), opacity 0.25s ease';
        albumContainer.style.transform = `translateX(${exitX})`;
        albumContainer.style.opacity = '0';

        albumContainer.addEventListener('transitionend', function handler() {
            // Update month index
            if (direction === 'next') {
                currentMonthIndex++;
            } else {
                currentMonthIndex--;
            }
            updateDateDisplay();

            // Repopulate with new album content
            albumContainer.innerHTML = '';
            const newAlbumContent = createAlbumElement(allFolders[currentMonthIndex]);
            albumContainer.appendChild(newAlbumContent);
            lazyLoadImages();
            if (isSelectingPhotos) {
                updatePhotoSelectUI();
            }

            // --- Scroll to top INSTANTLY before new content slides in ---
            window.scrollTo({
                top: 0,
                behavior: 'instant'
            });

            // Position for entry animation (instantly, no transition)
            albumContainer.style.transition = 'none';
            albumContainer.style.transform = `translateX(${entryX})`;
            albumContainer.style.opacity = '1';

            // Force browser to apply styles before adding transition back
            void albumContainer.offsetHeight;

            // Animate in the new album with a springy effect
            albumContainer.style.transition = 'transform 0.4s cubic-bezier(0.2, 0.8, 0.2, 1)';
            albumContainer.style.transform = 'translateX(0)';

            // 恢復滾動條
            albumContainer.addEventListener('transitionend', function endAnimation() {
                document.body.style.overflowX = '';
                albumContainer.removeEventListener('transitionend', endAnimation);
            }, { once: true });

        }, { once: true });
    }
    // --- END: Swipe navigation for albums ---

    // 定義節流函數，用於限制函數的執行頻率
    // @param func: 要執行的函數
    // @param delay: 延時間(毫秒)
    // @returns: 包裝後的節流函數
    function throttle(func, delay) {
        let lastCall = 0;
        return function (...args) {
            const now = new Date().getTime();
            if (now - lastCall < delay) {
                return;
            }
            lastCall = now;
            return func(...args);
        };
    }

    /**
     * 載入相簿資料
     * - 從伺服器獲取相簿資料
     * - 依日期排序
     * - 分頁顯示
     */
    function loadAlbums() {
        if (isLoading || allPhotosLoaded) return;
        isLoading = true;
        showLoadingAnimation();

        fetch('https://student.wentzao.com/get_photo_data', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                folder_ids: window.folderIdList || []
            })
        })
            .then(response => response.json())
            .then(responseData => {
                if (!responseData.photos || !Array.isArray(responseData.photos)) {
                    throw new Error('回應資料格式錯誤');
                }

                const groupedPhotos = groupPhotosByDate(responseData.photos);
                allFolders = groupedPhotos;
                allPhotosLoaded = true;
                initYearToolbar(); // 初始化年份工具列
                displayAlbums();
                window.folderIdList = responseData.folder_ids;
                console.log(window.folderIdList);
            })
            .catch(error => {
                console.error('Load Albums Error:', error);
            })
            .finally(() => {
                isLoading = false;
                hideLoadingAnimation();
            });
    }

    // 照片分組函數
    function groupPhotosByDate(photos) {
        const groups = new Map();

        photos.forEach(photo => {
            // 確保我們使用正確的日期和URL
            const date = new Date(photo.date);
            const monthKey = date.toISOString().slice(0, 7); // 格式如 "2024-10"

            if (!groups.has(monthKey)) {
                groups.set(monthKey, {
                    date: monthKey,
                    photos: []
                });
            }

            // 使用完整的 URL 和 photoId
            groups.get(monthKey).photos.push({
                url: photo.url,
                photoId: photo.photoId,
                name: photo.name,
                date: photo.date // Store full date for sorting favorites
            });
        });

        return Array.from(groups.values())
            .sort((a, b) => b.date.localeCompare(a.date));
    }

    /**
     * 顯示相簿
     * - 處理相簿的頁顯示
     * - 觸發延遲載入
     */
    function displayAlbums() {
        const container = document.getElementById('albums-container');

        // 檢查是否已經顯示了相同的相簿
        const currentFolder = allFolders[currentMonthIndex];
        const existingAlbum = container.querySelector('.album');

        if (existingAlbum) {
            const existingDate = existingAlbum.querySelector('.album-date').textContent;
            const newDate = new Date(currentFolder.date + '-01').toLocaleDateString('zh-TW', {
                year: 'numeric',
                month: 'long'
            });

            // 如果顯示的是相同的相簿，則不重新載入
            if (existingDate === newDate) {
                updateDateDisplay();
                if (isSelectingPhotos) {
                    updatePhotoSelectUI();
                }
                return;
            }

            existingAlbum.style.opacity = '0';
        }

        // 短暫延遲後更新內容
        setTimeout(() => {
            container.innerHTML = '';

            if (allFolders.length === 0) return;

            const albumElement = createAlbumElement(currentFolder);
            container.appendChild(albumElement);

            // 確保新內容淡入
            requestAnimationFrame(() => {
                albumElement.style.opacity = '1';
            });

            updateDateDisplay();
            lazyLoadImages();
            if (isSelectingPhotos) {
                updatePhotoSelectUI();
            }
        }, 150);
    }

    /**
     * 判斷照片是否為影片類型
     * @param {Object} photo - 照片對象
     * @returns {Boolean} 是否為影片
     */
    function isVideoItem(photo) {
        // 檢查明確的類型標記
        if (photo.type === 'video') {
            return true;
        }

        // 檢查文件名是否以視頻擴展名結尾
        if (photo.name) {
            const lowerName = photo.name.toLowerCase();
            if (lowerName.endsWith('.mp4') ||
                lowerName.endsWith('.mov') ||
                lowerName.endsWith('.avi') ||
                lowerName.endsWith('.wmv') ||
                lowerName.endsWith('.mkv')) {
                return true;
            }
        }

        return false;
    }

    function createAlbumElement(folder) {
        const album = document.createElement('div');
        album.classList.add('album', 'active');

        const formattedDate = folder.isFavorites ? '我的收藏' : new Date(folder.date + '-01').toLocaleDateString('zh-TW', {
            year: 'numeric',
            month: 'long'
        });

        album.innerHTML += `
            <div class="album-title">
                <span class="album-date">${formattedDate}</span>
            </div>
        `;

        console.log("相簿資料:", folder.photos);

        if (folder.photos && folder.photos.length > 0) {
            const photoGrid = document.createElement('div');
            photoGrid.classList.add('photo-grid');
            const isMobile = window.matchMedia('(max-width: 768px)').matches;
            const totalPhotos = folder.photos.length;

            const favoriteIconSvg = `
                <svg class="favorite-icon" viewBox="0 0 24 24">
                    <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"></path>
                </svg>
            `;

            if (isMobile) {
                // 手機版的照片網格邏輯
                for (let i = 0; i < totalPhotos; i += 6) {
                    const groupPhotos = folder.photos.slice(i, i + 6);
                    if (groupPhotos.length >= 4) {
                        const validPositions = [1, 2, 4, 5];
                        const randomPosition = validPositions[Math.floor(Math.random() * validPositions.length)];
                        const largePhotoIndex = i + (randomPosition - 1);

                        groupPhotos.forEach((photo, groupIndex) => {
                            const photoItem = document.createElement('div');
                            photoItem.classList.add('photo-item');

                            // 判斷是否為影片類型
                            const isVideo = isVideoItem(photo);
                            if (isVideo) {
                                console.log("發現影片:", photo);
                                photoItem.classList.add('video');
                                const videoIcon = document.createElement('div');
                                videoIcon.className = 'video-icon';
                                photoItem.appendChild(videoIcon);
                            }

                            const currentAbsoluteIndex = i + groupIndex;
                            const isLarge = currentAbsoluteIndex === largePhotoIndex && currentAbsoluteIndex < totalPhotos - 3;

                            if (isLarge) {
                                photoItem.classList.add('large');
                            }
                            if (favoritePhotos.has(photo.photoId)) {
                                photoItem.classList.add('favorited');
                            }

                            const img = document.createElement('img');
                            img.setAttribute('data-src', photo.url + (isLarge ? '=s400-c' : '=s300-c'));
                            img.setAttribute('data-full', photo.url);
                            img.setAttribute('data-photoId', photo.photoId);
                            img.setAttribute('data-name', photo.name);
                            // 添加照片類型屬性
                            img.setAttribute('data-type', isVideo ? 'video' : 'photo');
                            const currentPhotoIndex = folder.photos.indexOf(photo);
                            img.addEventListener('click', () => openFullscreen(folder.photos, currentPhotoIndex));

                            photoItem.innerHTML += favoriteIconSvg; // 添加收藏圖標
                            photoItem.appendChild(img); // 然後添加圖片以保留事件監聽器
                            photoGrid.appendChild(photoItem);
                        });
                    } else {
                        groupPhotos.forEach((photo, groupIndex) => {
                            const photoItem = document.createElement('div');
                            photoItem.classList.add('photo-item');

                            // 判斷是否為影片類型
                            const isVideo = isVideoItem(photo);
                            if (isVideo) {
                                console.log("發現影片:", photo);
                                photoItem.classList.add('video');
                                const videoIcon = document.createElement('div');
                                videoIcon.className = 'video-icon';
                                photoItem.appendChild(videoIcon);
                            }

                            if (favoritePhotos.has(photo.photoId)) {
                                photoItem.classList.add('favorited');
                            }

                            const img = document.createElement('img');
                            img.setAttribute('data-src', photo.url + '=s300-c');
                            img.setAttribute('data-full', photo.url);
                            img.setAttribute('data-photoId', photo.photoId);
                            img.setAttribute('data-name', photo.name);
                            // 添加照片類型屬性
                            img.setAttribute('data-type', isVideo ? 'video' : 'photo');
                            const currentPhotoIndex = folder.photos.indexOf(photo);
                            img.addEventListener('click', () => openFullscreen(folder.photos, currentPhotoIndex));

                            photoItem.innerHTML += favoriteIconSvg; // 添加收藏圖標
                            photoItem.appendChild(img); // 然後添加圖片以保留事件監聽器
                            photoGrid.appendChild(photoItem);
                        });
                    }
                }
            } else {
                // 桌面版的照片網格邏輯
                let largePhotoIndices = [];

                // 定義不同的排列模式（每15張照片中選3張作為大圖）
                const layoutPatterns = [
                    [0, 7, 9],
                    [2, 9, 10],
                    [1, 3, 8],
                    [0, 5, 10],
                    [2, 5, 9],
                    [0, 3, 8],
                    [1, 6, 8]
                ];

                if (totalPhotos < 10) {
                    // 當照片數量少於10張時，從前面的照片中隨機選擇一張作為大圖
                    const availableIndices = [];
                    for (let i = 0; i < totalPhotos - 4; i++) {
                        availableIndices.push(i);
                    }
                    if (availableIndices.length > 0) {
                        const randomIndex = availableIndices[Math.floor(Math.random() * availableIndices.length)];
                        largePhotoIndices.push(randomIndex);
                    }
                } else {
                    // 當照片數量大於等於10張時，使用新的15張一組的排列方式
                    for (let groupStart = 0; groupStart < totalPhotos; groupStart += 15) {
                        const groupEnd = Math.min(groupStart + 15, totalPhotos);
                        const groupSize = groupEnd - groupStart;

                        // 如果這一組的照片數量少於最後4張（避免最後4張被選為大圖）
                        const availableInGroup = Math.max(0, groupSize - 4);

                        if (availableInGroup >= 3) {
                            // 隨機選擇一個排列模式
                            const randomPattern = layoutPatterns[Math.floor(Math.random() * layoutPatterns.length)];

                            // 將模式中的相對索引轉換為絕對索引，並確保不超出可用範圍
                            randomPattern.forEach(relativeIndex => {
                                const absoluteIndex = groupStart + relativeIndex;
                                if (absoluteIndex < groupStart + availableInGroup && absoluteIndex < totalPhotos) {
                                    largePhotoIndices.push(absoluteIndex);
                                }
                            });
                        } else if (availableInGroup > 0) {
                            // 如果可用照片少於3張，隨機選擇一張
                            const randomIndex = groupStart + Math.floor(Math.random() * availableInGroup);
                            largePhotoIndices.push(randomIndex);
                        }
                    }
                }

                folder.photos.forEach((photo, index) => {
                    const photoItem = document.createElement('div');
                    photoItem.classList.add('photo-item');

                    // 判斷是否為影片類型
                    const isVideo = isVideoItem(photo);
                    if (isVideo) {
                        console.log("發現影片:", photo);
                        photoItem.classList.add('video');
                        const videoIcon = document.createElement('div');
                        videoIcon.className = 'video-icon';
                        photoItem.appendChild(videoIcon);
                    }

                    const isLarge = largePhotoIndices.includes(index);

                    if (isLarge) {
                        photoItem.classList.add('large');
                    }
                    if (favoritePhotos.has(photo.photoId)) {
                        photoItem.classList.add('favorited');
                    }

                    const img = document.createElement('img');
                    img.setAttribute('data-src', photo.url + (isLarge ? '=s500-c' : '=s220-c'));
                    img.setAttribute('data-full', photo.url);
                    img.setAttribute('data-photoId', photo.photoId);
                    img.setAttribute('data-name', photo.name);
                    // 添加照片類型屬性
                    img.setAttribute('data-type', isVideo ? 'video' : 'photo');
                    img.addEventListener('click', () => openFullscreen(folder.photos, index));

                    photoItem.innerHTML += favoriteIconSvg; // 添加收藏圖標
                    photoItem.appendChild(img); // 然後添加圖片以保留事件監聽器
                    photoGrid.appendChild(photoItem);
                });
            }

            album.appendChild(photoGrid);
        }

        return album;
    }

    /**
     * 照片延遲載入
     */
    function lazyLoadImages() {
        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                const img = entry.target;
                if (entry.isIntersecting) {
                    if (!img.hasAttribute('src') && img.dataset.src) {
                        img.src = img.dataset.src;
                        img.onload = () => {
                            img.parentElement.classList.remove('loading');
                            img.classList.add('fade-in'); // 添加入果的類別
                            observer.unobserve(img); // 圖片載入後停止觀察
                        };
                    }
                }
            });
        }, {
            rootMargin: '50px 0px', // 減少預載範圍
            threshold: 0.1
        });

        const imgs = document.querySelectorAll('img[data-src]');
        imgs.forEach(img => {
            if (!img.hasAttribute('src')) {
                img.parentElement.classList.add('loading');
                observer.observe(img);
            }
        });
    }

    /**
     * 開啟全螢幕照片查看
     * @param {Array} photos - 照片陣列
     * @param {Number} index - 起始照片索引
     */
    function openFullscreen(photos, index) {
        document.body.classList.add('no-scroll');

        const overlay = document.querySelector('.fullscreen-overlay');
        const swiperWrapper = overlay.querySelector('.swiper-wrapper');
        swiperWrapper.innerHTML = '';

        console.log("開啟全螢幕，顯示第", index, "項:", photos[index]);

        // 初始只載當前和前後各2張圖片
        photos.forEach((photo, i) => {
            const shouldLoadContent = Math.abs(i - index) <= 0; // 只預先載入當前幻燈片
            const slide = document.createElement('div');
            slide.classList.add('swiper-slide');
            slide.setAttribute('data-index', i);

            // 檢查是否為影片類型
            const isVideo = isVideoItem(photo);
            console.log(`項目 ${i}: ${isVideo ? '影片' : '圖片'} ${photo.name}`);

            if (isVideo) {
                // 對於影片，只創建一個容器，稍後由 Swiper 事件處理
                slide.innerHTML = `<div class="video-container-placeholder" data-video-id="${photo.photoId}"></div>`;
            } else {
                slide.innerHTML = `
                <div class="swiper-zoom-container">
                        <img src="${shouldLoadContent ? photo.url : ''}"
                         data-src="${photo.url}"
                         data-photoId="${photo.photoId}"
                         data-name="${photo.name}"
                             data-type="${isVideo ? 'video' : 'photo'}"
                             class="${shouldLoadContent ? 'swiper-lazy-loaded' : 'swiper-lazy'}" />
                </div>
            `;
            }

            swiperWrapper.appendChild(slide);
        });

        overlay.style.display = 'block';

        // 綁定滑動關閉事件
        overlay.addEventListener('touchstart', handleFullscreenTouchStart, { passive: false });
        overlay.addEventListener('touchmove', handleFullscreenTouchMove, { passive: false });
        overlay.addEventListener('touchend', handleFullscreenTouchEnd, { passive: false });

        if (swiper) {
            swiper.destroy(true, true);
        }

        swiper = new Swiper('.swiper', {
            initialSlide: index,
            zoom: { maxRatio: 3 },
            navigation: {
                nextEl: '.swiper-button-next',
                prevEl: '.swiper-button-prev',
            },
            preloadImages: false,
            lazy: {
                loadPrevNext: true,
                loadPrevNextAmount: 2,
            },
            photoData: photos, // 將照片數據傳遞給Swiper實例
            touchStartPreventDefault: false, // 重要：不阻止默認的觸摸事件
            threshold: 10, // 減小滑動閾值，讓滑動更靈敏
            touchRatio: 1, // 觸摸比例
            touchAngle: 45, // 觸摸角度
            on: {
                slideChange: function () {
                    const activeIndex = this.activeIndex;
                    const slides = this.slides;
                    const favoriteButton = document.querySelector('.favorite-button');
                    const currentPhoto = this.params.photoData[activeIndex];

                    console.log(`幻燈片切換到 ${activeIndex}`);

                    // 先設置收藏按鈕狀態
                    if (favoriteButton) {
                        if (currentPhoto && favoritePhotos.has(currentPhoto.photoId)) {
                            favoriteButton.classList.add('active');
                        } else {
                            favoriteButton.classList.remove('active');
                        }
                    }

                    // 檢查當前幻燈片是否為影片
                    const isVideo = isVideoItem(this.params.photoData[activeIndex]);
                    const lineButton = document.querySelector('.line-button');
                    const shareButton = document.querySelector('.share-button');

                    // 設置按鈕顯示狀態
                    if (isVideo) {
                        if (lineButton) lineButton.style.display = 'none';
                        // 設置分享按鈕文字為「分享影片」
                        if (shareButton) {
                            shareButton.textContent = '分享影片';
                            shareButton.setAttribute('aria-label', '分享影片');
                        }
                    } else {
                        if (lineButton) lineButton.style.display = 'block';
                        if (shareButton && liff.isInClient()) shareButton.style.display = 'block';
                        // 設置分享按鈕文字為原始文字
                        if (shareButton) {
                            shareButton.textContent = '分享照片';
                            shareButton.setAttribute('aria-label', '分享照片');
                        }

                        // 如果切換到非影片幻燈片，暫停當前播放的影片
                        if (globalVideoPlayer && globalVideoPlayer.playing) {
                            console.log('切換到照片幻燈片，暫停影片播放');
                            globalVideoPlayer.pause();

                            // 如果播放器有顯示容器，將其隱藏
                            if (globalVideoPlayer.elements && globalVideoPlayer.elements.container) {
                                globalVideoPlayer.elements.container.style.display = 'none';
                            }
                        }
                    }

                    if (!swiper) return;

                    // 取消任何待處理的狀態重置計時器
                    if (swiper.resetStateTimer) {
                        clearTimeout(swiper.resetStateTimer);
                        swiper.resetStateTimer = null;
                    }

                    // 標記幻燈片正在切換
                    swiper.isChangingSlide = true;

                    // 處理照片延遲加載 (只載入當前幻燈片)
                    const currentSlide = slides[activeIndex];
                    if (currentSlide) {
                        // 圖片延遲加載
                        const img = currentSlide.querySelector('img');
                        if (img && !img.src && img.dataset.src) {
                            img.src = img.dataset.src;
                            img.classList.remove('swiper-lazy');
                            img.classList.add('swiper-lazy-loaded');
                        }
                    }

                    // 如果當前幻燈片是影片，則加載它
                    if (isVideo && currentSlide) {
                        // 使用setTimeout以確保UI更新後再加載播放器
                        // 這可以減少競爭條件
                        setTimeout(() => {
                            // 檢查當前活動幻燈片是否仍然是我們期望的
                            if (swiper && swiper.activeIndex === activeIndex) {
                                console.log(`開始載入幻燈片 ${activeIndex} 的影片`);

                                // 獲取當前影片ID
                                const videoId = currentSlide.querySelector('.video-container-placeholder')?.dataset.videoId;

                                // 強制重置當前影片ID，確保會重新載入
                                currentVideoId = null;

                                // 如果播放器有顯示容器，確保它是可見的
                                if (globalVideoPlayer && globalVideoPlayer.elements &&
                                    globalVideoPlayer.elements.container) {
                                    globalVideoPlayer.elements.container.style.display = 'block';
                                }

                                // 載入並自動播放影片
                                loadPlyrPlayer(currentSlide);
                            }
                        }, 10); // 短延遲，確保畫面更新後再載入
                    }

                    // 無論是否為影片，延遲重置切換狀態
                    // 這確保了即使在快速連續滑動時，也能有足夠時間處理載入邏輯
                    swiper.resetStateTimer = setTimeout(() => {
                        if (swiper) swiper.isChangingSlide = false;
                        console.log(`幻燈片 ${activeIndex} 狀態已重置`);
                    }, 50);
                },
                init: function () {
                    // 初始化完成後確保當前幻燈片的影片已載入
                    const activeIndex = this.activeIndex;
                    const currentSlide = this.slides[activeIndex];
                    if (!currentSlide) return;

                    const isVideo = isVideoItem(this.params.photoData[activeIndex]);
                    if (isVideo) {
                        // 如果有全局播放器且有顯示容器，確保它是可見的
                        if (globalVideoPlayer && globalVideoPlayer.elements &&
                            globalVideoPlayer.elements.container) {
                            globalVideoPlayer.elements.container.style.display = 'block';
                        }

                        loadPlyrPlayer(currentSlide);
                        const lineButton = document.querySelector('.line-button');
                        const shareButton = document.querySelector('.share-button');

                        // 只隱藏下載按鈕，保留收藏和分享按鈕
                        lineButton.style.display = 'none';

                        // 設置分享按鈕文字為「分享影片」
                        if (shareButton) {
                            shareButton.textContent = '分享影片';
                            shareButton.setAttribute('aria-label', '分享影片');
                        }
                    }
                },
                touchStart: function (swiper, event) {
                    const slide = swiper.slides[swiper.activeIndex];
                    if (slide && slide.player && slide.player.playing) {
                        // 如果是影片並且正在播放，則暫停
                        slide.player.pause();
                        slide.isPausedByTouch = true; // 標記為觸摸暫停
                        event.preventDefault(); // 阻止滑動
                    }
                },
                touchEnd: function (swiper, event) {
                    const slide = swiper.slides[swiper.activeIndex];
                    if (slide && slide.player && slide.isPausedByTouch) {
                        // 如果是觸摸暫停的，則恢復播放
                        slide.player.play();
                        slide.isPausedByTouch = false;
                    }
                }
            }
        });

        document.addEventListener('keydown', handleKeydown);
        setupFullscreenButtons(photos);
        // Initial favorite button state
        const initialPhoto = photos[index];
        const favoriteButton = document.querySelector('.favorite-button');
        if (favoriteButton) {
            if (initialPhoto && favoritePhotos.has(initialPhoto.photoId)) {
                favoriteButton.classList.add('active');
            } else {
                favoriteButton.classList.remove('active');
            }
        }

        // 檢查初始顯示的是否為影片，如果是則隱藏按鈕
        const isInitialVideo = isVideoItem(photos[index]);
        const lineButton = document.querySelector('.line-button');
        const shareButton = document.querySelector('.share-button');

        if (isInitialVideo) {
            // 只隱藏下載按鈕，保留收藏和分享按鈕
            lineButton.style.display = 'none';

            // 設置分享按鈕文字為「分享影片」
            if (shareButton) {
                shareButton.textContent = '分享影片';
                shareButton.setAttribute('aria-label', '分享影片');
            }
        } else {
            // 設置分享按鈕文字為原始文字
            if (shareButton) {
                shareButton.textContent = '分享照片';
                shareButton.setAttribute('aria-label', '分享照片');
            }
        }
    }

    /**
     * 載入 Plyr 播放器 - 使用全局單一實例方式
     * @param {HTMLElement} slide - The swiper slide element
     */
    function loadPlyrPlayer(slide) {
        // 檢查slide是否存在
        if (!slide) return;

        // 檢查 Swiper 是否正在關閉
        if (swiper && swiper.isClosing) {
            console.log('Swiper 正在關閉，取消載入播放器');
            return;
        }

        const placeholder = slide.querySelector('.video-container-placeholder');
        if (!placeholder) return;

        const videoId = placeholder.dataset.videoId;
        if (!videoId) return;

        // 如果是相同的影片ID且播放器已存在且播放器有效，嘗試恢復播放
        if (videoId === currentVideoId && globalVideoPlayer && globalVideoPlayer.media) {
            console.log(`影片 ${videoId} 已經載入，嘗試恢復播放`);

            // 確保播放器容器在當前幻燈片中
            if (globalVideoPlayer.elements.container) {
                globalVideoPlayer.elements.container.style.display = 'block';
                if (globalVideoPlayer.elements.container.parentNode !== placeholder) {
                    placeholder.appendChild(globalVideoPlayer.elements.container);
                }
            }

            // 檢查影片元素是否有效源
            const videoElement = globalVideoPlayer.media;
            if (videoElement && videoElement.src) {
                // 恢復播放
                globalVideoPlayer.play().catch(e => {
                    console.log('恢復播放失敗，重新載入影片:', e);
                    // 如果播放失敗，重置ID以觸發重新載入
                    currentVideoId = null;
                });
            } else {
                // 如果影片元素沒有有效的源，重置ID以觸發重新載入
                console.log('影片源無效，需要重新載入');
                currentVideoId = null;
            }

            // 如果ID被重置為null，不返回，繼續執行重新載入邏輯
            if (videoId === currentVideoId) {
                return;
            }
        }

        // 更新當前影片ID
        currentVideoId = videoId;

        // 標記此 slide 正在載入中
        slide.isLoading = true;

        // 創建或顯示載入指示器
        let loadingIndicator = placeholder.querySelector('.video-loading-overlay');
        if (!loadingIndicator) {
            loadingIndicator = document.createElement('div');
            loadingIndicator.className = 'video-loading-overlay';
            loadingIndicator.innerHTML = '<div class="logo-spinner glow"><img src="/static/img/logo/logo-500.png" alt="Logo" width="120"></div><span>影片載入中...</span>';
            placeholder.innerHTML = ''; // 清空 placeholder
            placeholder.appendChild(loadingIndicator); // 加入載入指示
        } else {
            loadingIndicator.style.display = 'block';
        }

        console.log(`開始載入影片 ${videoId}`);

        // 使用帶重試機制的 API key 獲取函數
        getApiKeyWithRetry()
            .then(result => {
                const { apiKey, proxyUrl } = result;

                // 再次檢查 Swiper 狀態和當前幻燈片
                if (swiper && swiper.isClosing) {
                    console.log('Swiper 狀態已變更，取消影片載入');
                    slide.isLoading = false;
                    return;
                }

                // 檢查這個幻燈片是否還是當前活動的幻燈片
                const isActiveSlide = swiper &&
                    Array.from(swiper.slides).indexOf(slide) === swiper.activeIndex;

                if (!isActiveSlide) {
                    console.log('幻燈片不再是當前活動幻燈片，取消載入');
                    slide.isLoading = false;
                    return;
                }

                // 構建影片 URL
                let videoUrl;
                if (proxyUrl) {
                    videoUrl = `${proxyUrl}?id=${videoId}&key=${apiKey}`;
                } else {
                    videoUrl = `https://www.googleapis.com/drive/v3/files/${videoId}?alt=media&key=${apiKey}`;
                }

                // 檢查全局播放器是否已存在
                if (globalVideoPlayer) {
                    console.log(`使用全局播放器更新影片源: ${videoId}`);

                    try {
                        // 先暫停當前播放
                        if (globalVideoPlayer.playing) {
                            globalVideoPlayer.pause();
                        }

                        // 重置播放器狀態
                        globalVideoPlayer.muted = false;

                        // 保存原始的事件監聽器
                        const originalMedia = globalVideoPlayer.media;

                        // 將播放器移動到當前幻燈片
                        if (globalVideoPlayer.elements.container &&
                            globalVideoPlayer.elements.container.parentNode !== placeholder) {
                            placeholder.appendChild(globalVideoPlayer.elements.container);
                        }

                        // 更換視訊來源
                        const videoElement = globalVideoPlayer.media;
                        if (videoElement) {
                            // 監聽載入事件
                            const canPlayHandler = () => {
                                console.log(`更新的影片 ${videoId} 可以播放了`);

                                // 隱藏載入指示器
                                if (loadingIndicator) {
                                    loadingIndicator.style.display = 'none';
                                }

                                // 處理直式影片的顯示樣式
                                handlePortraitVideo(globalVideoPlayer);

                                // 自動播放新影片
                                globalVideoPlayer.play().catch(e => {
                                    console.log(`自動播放被阻止: ${e}`);
                                    // 嘗試靜音播放
                                    globalVideoPlayer.muted = true;
                                    globalVideoPlayer.play().catch(e2 => {
                                        console.log(`靜音自動播放也被阻止: ${e2}`);
                                    });
                                });

                                // 清除事件監聽器
                                videoElement.removeEventListener('canplay', canPlayHandler);
                            };

                            // 添加canplay事件監聽
                            videoElement.addEventListener('canplay', canPlayHandler);

                            // 更新src（這會自動觸發加載）
                            videoElement.src = videoUrl;
                            console.log(`已更新影片源: ${videoId}`);
                        }

                        // 完成載入
                        slide.isLoading = false;
                        return;

                    } catch (error) {
                        console.error('更新影片源失敗，將重建播放器:', error);
                        // 銷毀播放器
                        try {
                            globalVideoPlayer.destroy();
                            globalVideoPlayer = null;
                        } catch (e) {
                            console.error('銷毀播放器時發生錯誤:', e);
                        }
                    }
                }

                // 如果沒有全局播放器或更新失敗，創建新播放器

                // 清空容器
                placeholder.innerHTML = '';

                // 重新添加載入指示器
                loadingIndicator = document.createElement('div');
                loadingIndicator.className = 'video-loading-overlay';
                loadingIndicator.innerHTML = '<div class="logo-spinner glow"><img src="/static/img/logo/logo-500.png" alt="Logo" width="120"></div><span>影片載入中...</span>';
                placeholder.appendChild(loadingIndicator);

                // 創建 video 元素
                const videoElement = document.createElement('video');
                videoElement.playsInline = true;
                videoElement.autoplay = false;

                // 監聽視頻元素的錯誤事件
                videoElement.addEventListener('error', (e) => {
                    console.error(`影片元素錯誤 (${videoId}):`, e);
                    handleVideoError(placeholder, `影片載入失敗 (錯誤碼: ${videoElement.error ? videoElement.error.code : 'unknown'})`);
                    slide.isLoading = false;
                });

                // 先添加到 DOM
                placeholder.appendChild(videoElement);

                // 使用 setTimeout 確保 DOM 更新後再設置 src
                setTimeout(() => {
                    // 再次檢查 Swiper 狀態
                    if (swiper && swiper.isClosing) {
                        console.log('Swiper 狀態已變更，中止影片載入');
                        if (videoElement.parentNode) {
                            videoElement.parentNode.removeChild(videoElement);
                        }
                        slide.isLoading = false;
                        return;
                    }

                    // 再次檢查這個幻燈片是否還是當前活動的幻燈片
                    const stillActiveSlide = swiper &&
                        Array.from(swiper.slides).indexOf(slide) === swiper.activeIndex;

                    if (!stillActiveSlide) {
                        console.log('幻燈片不再是當前活動幻燈片，中止載入');
                        if (videoElement.parentNode) {
                            videoElement.parentNode.removeChild(videoElement);
                        }
                        slide.isLoading = false;
                        return;
                    }

                    try {
                        videoElement.src = videoUrl;
                    } catch (e) {
                        console.error('設置影片來源時發生錯誤:', e);
                        handleVideoError(placeholder, '無法載入影片來源');
                        slide.isLoading = false;
                        return;
                    }

                    // 初始化 Plyr
                    try {
                        const player = new Plyr(videoElement, {
                            controls: ['play-large', 'progress', 'mute', 'volume'],
                            autoplay: true,
                            muted: false,
                            volume: 1.0,
                            loadSprite: false,
                            loop: { active: true }
                        });

                        // 使用全局播放器實例
                        globalVideoPlayer = player;

                        // 播放器事件監聽
                        player.on('loadstart', () => {
                            console.log(`影片 ${videoId} 開始載入`);
                        });

                        player.on('ended', () => {
                            console.log(`影片 ${videoId} 播放結束，準備循環播放`);
                            player.restart();
                        });

                        player.on('muted', () => {
                            if (!player.muted) {
                                console.log('用戶已取消靜音');
                                player.userUnmuted = true;
                            }
                        });

                        player.on('canplay', () => {
                            console.log(`影片 ${videoId} 可以播放了`);
                            if (loadingIndicator) {
                                loadingIndicator.style.display = 'none';
                            }

                            // 檢查是否仍是當前幻燈片
                            const stillActiveSlide = swiper &&
                                Array.from(swiper.slides).indexOf(slide) === swiper.activeIndex;

                            if (stillActiveSlide) {
                                // 處理直式影片的顯示樣式
                                handlePortraitVideo(player);

                                player.muted = false;
                                player.play().catch(e => {
                                    console.log(`自動播放被阻止: ${e}`);
                                    player.muted = true;
                                    player.play().then(() => {
                                        setTimeout(() => {
                                            player.muted = false;
                                        }, 1000);
                                    }).catch(e2 => {
                                        console.log(`靜音自動播放也被阻止: ${e2}`);
                                    });
                                });
                            }
                        });

                        player.on('error', (event) => {
                            console.error(`影片 ${videoId} 播放器錯誤:`, event);
                            handleVideoError(placeholder, '播放器錯誤，請稍後再試');
                        });

                        // 手勢控制：按住暫停，放開播放
                        if (player.elements && player.elements.container) {
                            player.elements.container.addEventListener('mousedown', () => {
                                if (player.playing) {
                                    player.pause();
                                    player.isPausedByTouch = true;
                                }
                            });

                            player.elements.container.addEventListener('mouseup', () => {
                                if (player.isPausedByTouch) {
                                    player.play();
                                    player.isPausedByTouch = false;
                                }
                            });

                            player.elements.container.addEventListener('touchstart', (e) => {
                                if (player.playing) {
                                    player.pause();
                                    player.isPausedByTouch = true;
                                    e.preventDefault();
                                }
                            }, { passive: false });

                            player.elements.container.addEventListener('touchend', () => {
                                if (player.isPausedByTouch) {
                                    player.play();
                                    player.isPausedByTouch = false;
                                }
                            });
                        }
                    } catch (e) {
                        console.error('初始化播放器時發生錯誤:', e);
                        handleVideoError(placeholder, '無法初始化播放器');
                        slide.isLoading = false;
                    }
                }, 50);
            })
            .catch(error => {
                console.error('無法載入影片:', error);
                handleVideoError(placeholder, '無法載入影片，請稍後再試');
            })
            .finally(() => {
                // 確保重置載入中標記
                setTimeout(() => {
                    if (slide.isLoading) {
                        slide.isLoading = false;
                    }
                }, 100);
            });
    }

    /**
     * 處理影片錯誤
     * @param {HTMLElement} container - 影片容器
     * @param {string} message - 錯誤訊息
     */
    function handleVideoError(container, message) {
        if (!container) return;

        // 清除載入指示器
        const loadingIndicator = container.querySelector('.video-loading-overlay');
        if (loadingIndicator) {
            loadingIndicator.style.display = 'none';
        }

        // 顯示錯誤訊息
        let errorElement = container.querySelector('.video-error');
        if (!errorElement) {
            errorElement = document.createElement('div');
            errorElement.className = 'video-error';
            container.appendChild(errorElement);
        }
        errorElement.textContent = message || '無法載入影片，請稍後再試';

        // 添加重試按鈕
        const retryButton = document.createElement('button');
        retryButton.className = 'video-retry-button';
        retryButton.textContent = '重試';
        retryButton.style.marginTop = '10px';
        retryButton.style.padding = '5px 15px';
        retryButton.style.background = 'rgba(255, 255, 255, 0.2)';
        retryButton.style.border = 'none';
        retryButton.style.borderRadius = '4px';
        retryButton.style.color = 'white';
        retryButton.style.cursor = 'pointer';

        retryButton.addEventListener('click', () => {
            // 獲取 slide 元素
            const slide = container.closest('.swiper-slide');
            if (slide) {
                // 清除錯誤狀態
                container.innerHTML = '';
                // 重新載入播放器
                loadPlyrPlayer(slide);
            }
        });

        errorElement.appendChild(retryButton);
    }

    /**
     * 載入影片iframe，添加載入事件監聽
     * @param {HTMLElement} container - 影片容器元素
     */
    function loadVideoIframe(container) {
        if (!container || !container.dataset.videoId || container.querySelector('iframe')) {
            return; // 已經載入過或無需載入
        }

        const videoId = container.dataset.videoId;
        const placeholder = container.querySelector('.video-placeholder');

        // 創建iframe元素
        const iframe = document.createElement('iframe');
        iframe.src = `https://drive.google.com/file/d/${videoId}/preview`;
        iframe.setAttribute('allow', 'autoplay; fullscreen');
        iframe.setAttribute('allowfullscreen', 'true');
        iframe.style.opacity = '0'; // 一開始設為透明

        // 添加載入事件監聽
        iframe.onload = () => {
            if (placeholder) {
                // 淡出placeholder
                placeholder.style.opacity = '0';

                // 淡入iframe
                setTimeout(() => {
                    iframe.style.opacity = '1';

                    setTimeout(() => {
                        if (placeholder.parentNode === container) {
                            container.removeChild(placeholder);
                        }
                    }, 500);
                }, 200);
            }
            // 添加保護層
            const shield = document.createElement('div');
            shield.className = 'iframe-touch-shield';
            container.appendChild(shield);

            // 添加自定義全螢幕按鈕
            const fullscreenBtn = document.createElement('div');
            fullscreenBtn.className = 'video-fullscreen-btn';
            fullscreenBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M7,14H5v5h5v-2H7V14z M5,10h2V7h3V5H5V10z M17,17h-3v2h5v-5h-2V17z M14,5v2h3v3h2V5H14z"></path></svg>';
            container.appendChild(fullscreenBtn);

            // 自定義全螢幕按鈕事件
            fullscreenBtn.addEventListener('click', () => {
                if (iframe.requestFullscreen) {
                    iframe.requestFullscreen();
                } else if (iframe.webkitRequestFullscreen) {
                    iframe.webkitRequestFullscreen();
                } else if (iframe.msRequestFullscreen) {
                    iframe.msRequestFullscreen();
                } else if (container.requestFullscreen) { // 如果iframe不支持，嘗試容器
                    container.requestFullscreen();
                } else {
                    // 備用方案：開新窗口顯示影片
                    window.open(`https://drive.google.com/file/d/${videoId}/view`, '_blank');
                }
            });
        };

        // 添加iframe
        container.appendChild(iframe);
        container.removeAttribute('data-video-id');
    }

    /**
     * 當滑離影片幻燈片時，釋放資源
     * @param {HTMLElement} container - 影片容器元素
     */
    function unloadVideoIframe(container) {
        if (!container) return;

        const iframe = container.querySelector('iframe');
        if (iframe) {
            // 將src設為空以停止影片並釋放資源
            iframe.src = 'about:blank';
        }
    }

    function handleKeydown(e) {
        if (document.querySelector('.fullscreen-overlay').style.display !== 'block') return;

        switch (e.key) {
            case 'ArrowLeft':
                swiper.slidePrev();
                break;
            case 'ArrowRight':
                swiper.slideNext();
                break;
            case 'Escape':
                closeFullscreen();
                break;
            case 'f': // Add 'f' key to favorite
                const photos = swiper.params.photoData;
                const currentPhoto = photos[swiper.activeIndex];
                toggleFavoriteStatus(currentPhoto);
                break;
        }
    }

    /**
     * 設定螢幕模式的按鈕
     * @param {Array} photos - 照片陣列
     */
    function setupFullscreenButtons(photos) {
        const buttonContainer = document.querySelector('.button-container');
        const lineButton = document.querySelector('.line-button');
        const shareButton = document.querySelector('.share-button');
        const favoriteButton = document.querySelector('.favorite-button');

        // 創建下載選項容器
        let downloadOptions = buttonContainer ? buttonContainer.querySelector('.download-options') : null;
        if (!downloadOptions && lineButton) {
            downloadOptions = document.createElement('div');
            downloadOptions.className = 'download-options';
            downloadOptions.innerHTML = `
                <div class="download-option" data-quality="standard">標準畫質下載</div>
                <div class="download-option" data-quality="original">原始畫質下載</div>
            `;
            lineButton.style.position = 'relative';
            lineButton.appendChild(downloadOptions);
        }

        if (liff.isInClient()) {
            if (buttonContainer) buttonContainer.style.cssText = 'bottom: 2rem; left: 0; right: 0; justify-content: center;';
            if (lineButton) lineButton.style.display = 'block';
            if (shareButton) shareButton.style.display = 'block';
            if (favoriteButton) favoriteButton.style.display = 'block';
        } else {
            if (buttonContainer) buttonContainer.style.cssText = 'bottom: 2rem; right: 1rem; justify-content: flex-end;';
            if (lineButton) lineButton.style.display = 'block';
            if (shareButton) shareButton.style.display = 'none';
            if (favoriteButton) favoriteButton.style.display = 'block';
        }

        // 移除所有現有的事件監聽器
        if (lineButton) lineButton.replaceWith(lineButton.cloneNode(true));
        if (shareButton) shareButton.replaceWith(shareButton.cloneNode(true));
        if (favoriteButton) favoriteButton.replaceWith(favoriteButton.cloneNode(true));

        // 重新獲取新的元素引用
        const newLineButton = document.querySelector('.line-button');
        const newShareButton = document.querySelector('.share-button');
        const newFavoriteButton = document.querySelector('.favorite-button');
        downloadOptions = newLineButton ? newLineButton.querySelector('.download-options') : null;

        // 收藏按鈕點擊事件
        if (newFavoriteButton) {
            newFavoriteButton.addEventListener('click', () => {
                const currentPhoto = photos[swiper.activeIndex];
                toggleFavoriteStatus(currentPhoto);
            });
        }

        // 下載按鈕點擊事件
        newLineButton.addEventListener('click', (e) => {
            if (e.target === newLineButton) {
                downloadOptions.classList.toggle('show');
                e.stopPropagation();
            }
        });

        // 點擊其他地方關閉選項
        document.addEventListener('click', (e) => {
            if (newLineButton && !newLineButton.contains(e.target)) {
                downloadOptions.classList.remove('show');
            }
        });

        // 下載選項點擊事件
        downloadOptions.addEventListener('click', async (e) => {
            const option = e.target.closest('.download-option');
            if (!option) return;

            const quality = option.dataset.quality;
            const currentPhoto = photos[swiper.activeIndex];

            // 檢查是否為影片類型
            if (isVideoItem(currentPhoto)) {
                showNotification('影片不支援此下載方式，請使用分享功能');
                downloadOptions.classList.remove('show');
                return;
            }

            const baseUrl = `https://lh3.googleusercontent.com/d/${currentPhoto.photoId}`;
            const downloadUrl = quality === 'original' ? `${baseUrl}=s0` : baseUrl;

            try {
                showNotification('準備下載照片...');

                if (liff.isInClient()) {
                    // LIFF 環境使用 navigator.share
                    const response = await fetch(downloadUrl);
                    const blob = await response.blob();
                    const file = new File([blob], currentPhoto.name || 'photo.jpg', { type: 'image/jpeg' });

                    try {
                        await navigator.share({
                            files: [file]
                        });
                        showNotification('照片操作已完成');
                    } catch (shareError) {
                        if (shareError.name === 'AbortError') {
                            showNotification('已取消操作');
                        } else {
                            // 如果 navigator.share 失敗，改用 LIFF sendMessages
                            await liff.sendMessages([{
                                type: 'image',
                                originalContentUrl: downloadUrl,
                                previewImageUrl: currentPhoto.url + '=s300-c'
                            }]);
                            showNotification('照片已傳送到聊天室');
                        }
                    }
                } else {
                    // 桌面版使用直接下載
                    const response = await fetch(downloadUrl);
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const link = document.createElement('a');
                    link.href = url;
                    link.download = currentPhoto.name || 'photo.jpg';
                    document.body.appendChild(link);
                    link.click();
                    document.body.removeChild(link);
                    window.URL.revokeObjectURL(url);
                    showNotification('照片下載完成');
                }
            } catch (error) {
                console.error('照片操作失敗:', error);
                showNotification('照片操作失敗，請稍後再試');
            }

            downloadOptions.classList.remove('show');
        });

        // 分享按鈕點擊事件
        if (liff.isInClient()) {
            newShareButton.addEventListener('click', () => {
                const currentPhoto = photos[swiper.activeIndex];
                shareImage(currentPhoto);
            });
        }
    }

    /**
     * 關閉螢幕模式
     */
    function closeFullscreen() {
        // 先移除所有可能影響滾動的樣式
        document.body.classList.remove('no-scroll');

        // 清理 Swiper 和全局播放器
        if (swiper) {
            console.log('關閉全螢幕：開始清理資源');

            // 標記關閉狀態，防止新的資源載入
            swiper.isClosing = true;

            // 處理全局播放器
            if (globalVideoPlayer) {
                console.log('關閉全螢幕：暫停全局影片播放器');
                try {
                    // 強制暫停播放，確保不會在背景繼續播放
                    if (globalVideoPlayer.playing) {
                        globalVideoPlayer.pause();
                    }

                    // 移除影片源，徹底停止影片播放和資源消耗
                    const videoElement = globalVideoPlayer.media;
                    if (videoElement) {
                        try {
                            // 保存當前時間點，以便下次重新播放
                            const currentTime = videoElement.currentTime;
                            globalVideoPlayer.lastPlayTime = currentTime;

                            // 移除源和緩衝數據，確實停止播放
                            videoElement.pause();
                            videoElement.src = '';
                            videoElement.load(); // 釋放資源
                        } catch (ve) {
                            console.error('清理影片元素時發生錯誤:', ve);
                        }
                    }

                    // 記住當前的視頻ID，但重置為null表示需要重新載入
                    currentVideoId = null;

                    // 把播放器移到一個隱藏的地方保存，而不是銷毀它
                    if (globalVideoPlayer.elements.container &&
                        globalVideoPlayer.elements.container.parentNode) {
                        // 創建一個隱藏的容器來存放播放器
                        let hiddenContainer = document.getElementById('hidden-player-container');
                        if (!hiddenContainer) {
                            hiddenContainer = document.createElement('div');
                            hiddenContainer.id = 'hidden-player-container';
                            hiddenContainer.style.display = 'none';
                            document.body.appendChild(hiddenContainer);
                        }

                        // 移動播放器到隱藏容器
                        hiddenContainer.appendChild(globalVideoPlayer.elements.container);
                    }
                } catch (e) {
                    console.error('處理播放器時發生錯誤:', e);
                    // 如果發生錯誤，嘗試銷毀播放器
                    try {
                        globalVideoPlayer.destroy();
                    } catch (e2) {
                        console.error('銷毀播放器時發生錯誤:', e2);
                    }
                    globalVideoPlayer = null;
                    currentVideoId = null;
                }
            }

            // 清除所有幻燈片的加載狀態
            swiper.slides.forEach(slide => {
                slide.isLoading = false;

                // 清空視頻容器
                const placeholder = slide.querySelector('.video-container-placeholder');
                if (placeholder) {
                    placeholder.innerHTML = '';
                }
            });

            // 銷毀 Swiper 實例
            try {
                swiper.destroy(true, true);
            } catch (e) {
                console.error('銷毀 Swiper 時發生錯誤:', e);
            }
            swiper = null;
            console.log('關閉全螢幕：所有資源已釋放');
        }

        // 移除事件監聽器
        document.removeEventListener('keydown', handleKeydown);
        const overlay = document.querySelector('.fullscreen-overlay');
        overlay.removeEventListener('touchstart', handleFullscreenTouchStart);
        overlay.removeEventListener('touchmove', handleFullscreenTouchMove);
        overlay.removeEventListener('touchend', handleFullscreenTouchEnd);

        // 重設可能在滑動過程中被修改的樣式
        overlay.style.backgroundColor = 'rgba(0, 0, 0, 0.9)';
        const swiperContainer = document.querySelector('.swiper');
        if (swiperContainer) {
            swiperContainer.style.transform = '';
        }

        // 清空並隱藏 overlay
        const swiperWrapper = document.querySelector('.swiper-wrapper');
        if (swiperWrapper) {
            swiperWrapper.innerHTML = '';
        }
        overlay.style.display = 'none';

        // 執行垃圾回收提示
        if (window.gc) {
            try {
                window.gc();
                console.log('已請求執行垃圾回收');
            } catch (e) {
                console.log('垃圾回收不可用');
            }
        }

        // 延遲一小段時間後再進行其他操作，確保資源已釋放
        setTimeout(() => {
            if (isSelectingPhotos) {
                // 重新更新多選UI，確保顯示正確
                updatePhotoSelectUI();
            }
        }, 200);
    }

    /**
     * UI 相關功能
     * - 載入動畫
     * - 通知訊息
     * - 照片分享
     */
    function showLoadingAnimation() {
        document.getElementById('loading').style.display = 'block';
    }

    function hideLoadingAnimation() {
        document.getElementById('loading').style.display = 'none';
    }

    function showNotification(message) {
        const notification = document.querySelector('.custom-notification');
        notification.textContent = message;
        notification.style.display = 'block';
        setTimeout(() => {
            notification.style.display = 'none';
        }, 2000);
    }

    function shareImage(photo) {
        // 處理影片類型
        if (isVideoItem(photo)) {
            const videoUrl = `https://drive.google.com/file/d/${photo.photoId}/view`;
            if (liff.isInClient()) {
                showNotification('準備分享影片...');
                liff.shareTargetPicker([{
                    type: 'text',
                    text: `📽️請點擊連結查看影片：${videoUrl}?openExternalBrowser=1`
                }])
                    .then((result) => {
                        if (result) {
                            showNotification('影片連結已成功分享');
                        } else {
                            showNotification('分享已取消');
                        }
                    })
                    .catch(error => {
                        console.error('分享影片失敗:', error);
                        showNotification('分享影片失敗');
                    });
            } else {
                if (navigator.share) {
                    navigator.share({
                        title: '分享影片',
                        text: '查看這部影片',
                        url: videoUrl
                    })
                        .then(() => {
                            showNotification('影片已成功分享');
                        })
                        .catch(error => {
                            console.error('分享失敗:', error);
                            showNotification('分享影片失敗');
                        });
                } else {
                    const tempInput = document.createElement('input');
                    document.body.appendChild(tempInput);
                    tempInput.value = videoUrl;
                    tempInput.select();
                    document.execCommand('copy');
                    document.body.removeChild(tempInput);
                    showNotification('影片連結已複製到剪貼簿');
                }
            }
            return;
        }

        // 原有照片分享邏輯
        const baseUrl = `https://lh3.googleusercontent.com/d/${photo.photoId}=s0`;
        if (liff.isInClient()) {
            showNotification('準備分享...');
            liff.shareTargetPicker([{
                type: 'image',
                originalContentUrl: baseUrl,
                previewImageUrl: photo.url + '=s300-c'
            }])
                .then((result) => {
                    if (result) {
                        showNotification('照片已成功分享');
                    } else {
                        showNotification('分享已取消');
                    }
                })
                .catch(error => {
                    console.error('分享照片失敗:', error);
                    showNotification('分享照片失敗');
                });
        } else {
            if (navigator.share) {
                navigator.share({
                    title: '分享照片',
                    text: '查看這照片',
                    url: photo.url
                })
                    .then(() => {
                        showNotification('照片已成功分享');
                    })
                    .catch(error => {
                        console.error('分享失敗:', error);
                        showNotification('分享照片失敗');
                    });
            } else {
                const tempInput = document.createElement('input');
                document.body.appendChild(tempInput);
                tempInput.value = photo.url;
                tempInput.select();
                document.execCommand('copy');
                document.body.removeChild(tempInput);
                showNotification('照片連結已複製到剪貼簿');
            }
        }
    }

    /**
     * 事件監聽器設定
     * 1. 滾動件 - 實現無限滾動，並進行節流
     * 2. 關閉按鈕事件
     */
    // 無限滾動，並進行節流處理
    window.addEventListener('scroll', throttle(function () {
        if (isLoading || document.body.classList.contains('favorites-view-active')) return;

        // 計算滾動位置
        const scrollHeight = document.documentElement.scrollHeight;
        const scrollPosition = window.innerHeight + window.pageYOffset;
        const buffer = 100; // 緩衝距離

        if (scrollPosition + buffer >= scrollHeight) {
            displayAlbums(); // 直接呼叫 displayAlbums 來載入更多照片
        }
    }, 200)); // 節流時間為 200ms

    // 關閉按鈕事件
    document.querySelector('.close-button').addEventListener('click', closeFullscreen);

    function updateYearToolbarActiveState(activeYear) {
        document.querySelectorAll('.year-btn').forEach(btn => {
            btn.classList.remove('active');
        });

        const targetBtn = document.querySelector(`.year-btn[data-year="${activeYear}"]`);
        if (targetBtn) {
            targetBtn.classList.add('active');

            // 確保 active 按鈕在 year-scroll 的最左側，稍微往右偏移
            const yearScrollContainer = document.querySelector('.year-scroll');
            if (yearScrollContainer) {
                yearScrollContainer.scrollTo({
                    left: Math.max(0, targetBtn.offsetLeft - 40), // 往左偏移24px，讓按鈕不會貼邊
                    behavior: 'smooth'
                });
            }
        }
    }

    function initYearToolbar() {
        const years = new Set();
        // (Teacher version: no favorites)
        years.add('all'); // 添加"最新"選項
        years.add('oldest'); // 添加"最舊"選項

        // 從所有相簿中提取年份
        allFolders.forEach(folder => {
            const year = folder.date.substring(0, 4);
            years.add(year);
        });

        // 更新工具列按鈕
        const yearScroll = document.querySelector('.year-scroll');
        yearScroll.innerHTML = ''; // 清空現有按鈕

        // 添加所有按鈕
        Array.from(years).sort((a, b) => {
            if (a === 'favorites') return -1;
            if (b === 'favorites') return 1;
            if (a === 'all') return -1;
            if (b === 'all') return 1;
            if (a === 'oldest') return 1;
            if (b === 'oldest') return -1;
            return b.localeCompare(a);
        }).forEach(year => {
            const btn = document.createElement('button');
            btn.className = 'year-btn';
            btn.textContent = year === 'all' ? '最新' :
                year === 'oldest' ? '最舊' :
                    year === 'favorites' ? '我的收藏' :
                        year + '年';
            btn.dataset.year = year;
            if (year === 'all') btn.classList.add('active');
            yearScroll.appendChild(btn);
        });

        // 添加點擊事件處理
        yearScroll.addEventListener('click', e => {
            if (e.target.classList.contains('year-btn')) {
                // 更新按鈕狀態
                document.querySelectorAll('.year-btn').forEach(btn => {
                    btn.classList.remove('active');
                });
                e.target.classList.add('active');

                // 過濾並顯示相簿
                filterAlbumsByYear(e.target.dataset.year);
            }
        });
    }

    function filterAlbumsByYear(year) {
        document.body.classList.remove('favorites-view-active');
        document.querySelector('.date-bar').style.display = 'flex';

        if (year === 'favorites') {
            displayFavorites();
            return;
        }

        if (year === 'all') {
            currentMonthIndex = 0;
            displayAlbums();
            return;
        }

        if (year === 'oldest') {
            // 找到最後一個相簿的索引（最舊的相簿）
            currentMonthIndex = allFolders.length - 1;
            displayAlbums();
            return;
        }

        // 過濾指定年份的相簿
        const filteredFolders = allFolders.filter(folder =>
            folder.date.startsWith(year)
        );

        if (filteredFolders.length > 0) {
            currentMonthIndex = allFolders.findIndex(folder => folder === filteredFolders[0]);
            displayAlbums();
        }
    }

    function displayFavorites() {
        document.body.classList.add('favorites-view-active');
        const container = document.getElementById('albums-container');
        container.innerHTML = '';

        const favoritePhotoList = [];
        allFolders.forEach(folder => {
            folder.photos.forEach(photo => {
                if (favoritePhotos.has(photo.photoId)) {
                    favoritePhotoList.push(photo);
                }
            });
        });

        if (favoritePhotoList.length > 0) {
            // Sort by the date the photo was taken, newest first
            favoritePhotoList.sort((a, b) => new Date(b.date) - new Date(a.date));

            const favoritesAlbum = {
                date: new Date().toISOString().slice(0, 7), // Use a dummy date
                photos: favoritePhotoList,
                isFavorites: true
            };
            const albumElement = createAlbumElement(favoritesAlbum);
            container.appendChild(albumElement);
            lazyLoadImages();
            if (isSelectingPhotos) {
                updatePhotoSelectUI();
            }
        } else {
            container.innerHTML = '<p style="text-align: center; margin-top: 50px; color: #555;">快來收藏寶貝的照片吧！</p>';
        }
        updateDateDisplay();
    }

    function initializeUserProfile() {
        if (liff.isLoggedIn()) {
            liff.getProfile()
                .then(profile => {
                    userProfile = profile;
                    const profileImage = document.querySelector('.profile-image');
                    profileImage.innerHTML = `<img src="${profile.pictureUrl}" alt="用戶頭像">`;

                    // 添加點擊事件
                    profileImage.addEventListener('click', toggleLogoutPopup);
                    // 載入收藏的照片
                    loadFavoritePhotos();
                })
                .catch(console.error);
        }
    }

    function toggleLogoutPopup() {
        const popup = document.querySelector('.logout-popup');
        if (popup) popup.classList.toggle('show');
    }

    // 添加登出功能 (Teacher version: elements may not exist)
    const logoutBtn = document.querySelector('.logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', () => {
            if (liff.isLoggedIn()) {
                liff.logout();
                window.location.reload();
            }
        });
    }

    // 點擊其他地方關閉彈出框
    document.addEventListener('click', (e) => {
        const popup = document.querySelector('.logout-popup');
        const profileImage = document.querySelector('.profile-image');

        if (popup && profileImage && !profileImage.contains(e.target) && !popup.contains(e.target)) {
            popup.classList.remove('show');
        }
    });

    // 添加月份導航功能
    function updateMonthNavigation() {
        const prevBtn = document.querySelector('.prev-month');
        const nextBtn = document.querySelector('.next-month');

        // 更新上一月按鈕
        if (currentMonthIndex > 0) {
            const prevMonth = allFolders[currentMonthIndex - 1];
            const prevDate = new Date(prevMonth.date + '-01');
            prevBtn.querySelector('.month-label').textContent =
                prevDate.toLocaleDateString('zh-TW', { year: 'numeric', month: 'long' });
            prevBtn.style.display = 'flex';
        } else {
            prevBtn.style.display = 'none';
        }

        // 更新下一月按鈕
        if (currentMonthIndex < allFolders.length - 1) {
            const nextMonth = allFolders[currentMonthIndex + 1];
            const nextDate = new Date(nextMonth.date + '-01');
            nextBtn.querySelector('.month-label').textContent =
                nextDate.toLocaleDateString('zh-TW', { year: 'numeric', month: 'long' });
            nextBtn.style.display = 'flex';
        } else {
            nextBtn.style.display = 'none';
        }
    }

    // 修改月份切換事件處理
    document.querySelector('.date-bar .prev-month').addEventListener('click', function (e) {
        e.preventDefault(); // 防止事件冒泡

        const datePicker = document.querySelector('.date-picker');
        if (datePicker.classList.contains('show')) {
            hideDatePicker();
        }

        if (currentMonthIndex > 0) {
            changeMonthWithAnimation('prev');
        } else if (currentMonthIndex === 0 && !document.body.classList.contains('favorites-view-active')) {
            changeToFavoritesWithAnimation();
        }
    });

    document.querySelector('.date-bar .next-month').addEventListener('click', function (e) {
        e.preventDefault(); // 防止事件冒泡

        const datePicker = document.querySelector('.date-picker');
        if (datePicker.classList.contains('show')) {
            hideDatePicker();
        }

        if (document.body.classList.contains('favorites-view-active')) {
            changeFromFavoritesWithAnimation();
        } else if (currentMonthIndex < allFolders.length - 1) {
            changeMonthWithAnimation('next');
        }
    });

    // 在 DOMContentLoaded 事件添加
    let lastScrollTop = 0;
    const yearToolbar = document.querySelector('.year-toolbar');
    const dateBar = document.querySelector('.date-bar');

    // 更新日期顯示
    function updateDateDisplay() {
        const prevBtn = document.querySelector('.date-bar .prev-month');
        const nextBtn = document.querySelector('.date-bar .next-month');
        const currentDateElement = document.querySelector('.current-date');

        document.querySelector('.date-bar').style.display = 'flex';

        if (document.body.classList.contains('favorites-view-active')) {
            currentDateElement.textContent = '我的收藏';

            // 在我的收藏中，左邊按鈕隱藏
            prevBtn.classList.add('inactive');
            prevBtn.innerHTML = ''; // 清空內容

            // 右邊按鈕顯示最新相簿，引導返回
            if (allFolders.length > 0) {
                const latestFolder = allFolders[0];
                const date = new Date(latestFolder.date + '-01');
                const formattedDate = date.toLocaleDateString('zh-TW', { year: 'numeric', month: 'long' });
                nextBtn.innerHTML = `
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M8.59 16.59L10 18L16 12L10 6L8.59 7.41L13.17 12L8.59 16.59Z" fill="currentColor"></path>
                    </svg>
                `;
                nextBtn.classList.remove('inactive');
            } else {
                nextBtn.classList.add('inactive');
            }
            updateYearToolbarActiveState('favorites');
            return;
        }

        if (!allFolders || allFolders.length === 0) return;

        const currentFolder = allFolders[currentMonthIndex];
        const date = new Date(currentFolder.date + '-01');
        const formattedDate = date.toLocaleDateString('zh-TW', {
            year: 'numeric',
            month: 'long'
        });

        currentDateElement.textContent = formattedDate;

        // 更新導航按鈕狀態
        if (currentMonthIndex === 0) {
            // 在最新月份時，左邊顯示愛心圖示
            prevBtn.innerHTML = `<svg class="favorite-icon" viewBox="0 0 24 24" style="width: 28px; height: 28px; margin: 0 auto; fill: transparent; stroke: #555; stroke-width: 2;">
                <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"></path>
            </svg>`;
            prevBtn.classList.remove('inactive');
        } else {
            // 其他月份正常顯示
            const prevMonth = allFolders[currentMonthIndex - 1];
            const prevDate = new Date(prevMonth.date + '-01');
            const prevFormattedDate = prevDate.toLocaleDateString('zh-TW', { year: 'numeric', month: 'long' });
            prevBtn.innerHTML = `
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M15.41 7.41L14 6L8 12L14 18L15.41 16.59L10.83 12L15.41 7.41Z" fill="currentColor"></path>
                </svg>`;
            prevBtn.classList.remove('inactive');
        }

        if (currentMonthIndex === allFolders.length - 1) {
            nextBtn.classList.add('inactive');
        } else {
            const nextMonth = allFolders[currentMonthIndex + 1];
            const nextDate = new Date(nextMonth.date + '-01');
            const nextFormattedDate = nextDate.toLocaleDateString('zh-TW', { year: 'numeric', month: 'long' });
            nextBtn.innerHTML = `
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M8.59 16.59L10 18L16 12L10 6L8.59 7.41L13.17 12L8.59 16.59Z" fill="currentColor"></path>
                </svg>`;
            nextBtn.classList.remove('inactive');
        }

        // 同步更新年份工具列
        if (currentMonthIndex === 0) {
            updateYearToolbarActiveState('all');
        } else if (currentMonthIndex === allFolders.length - 1) {
            updateYearToolbarActiveState('oldest');
        } else {
            const currentYear = currentFolder.date.substring(0, 4);
            updateYearToolbarActiveState(currentYear);
        }
    }

    // 處理滾動事件
    window.addEventListener('scroll', throttle(() => {
        const st = window.pageYOffset || document.documentElement.scrollTop;

        if (st > lastScrollTop && st > 52) {
            // 向下滾動
            yearToolbar.classList.add('hidden');
            dateBar.classList.add('visible');
        } else if (st < lastScrollTop || st === 0) {
            // 向上滾動或到頂
            yearToolbar.classList.remove('hidden');
            dateBar.classList.remove('visible');
        }

        lastScrollTop = st;
    }, 100));

    // 修改滾動處理函數
    let scrollTimeout;
    window.addEventListener('scroll', function () {
        clearTimeout(scrollTimeout);

        const dateBar = document.querySelector('.date-bar');
        if (dateBar) {
            // 只在桌面版確保工具列保持在中間，手機版使用 CSS 定位
            const isMobile = window.matchMedia('(max-width: 768px)').matches;
            if (!isMobile) {
                dateBar.style.transform = 'translateX(-50%)';
            }
            dateBar.style.transition = 'none'; // 暫時移除過渡效果

            // 重新啟用過渡效果
            scrollTimeout = setTimeout(function () {
                dateBar.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)';
            }, 50);
        }
    }, { passive: true });

    // 在頁面載入時就設定好初始狀態
    document.addEventListener('DOMContentLoaded', function () {
        const dateBar = document.querySelector('.date-bar');
        if (dateBar) {
            // 只在桌面版設定居中，手機版使用 CSS 定位
            const isMobile = window.matchMedia('(max-width: 768px)').matches;
            if (!isMobile) {
                dateBar.style.transform = 'translateX(-50%)';
            }
        }
    });

    // 電子賀卡相關功能
    function initializeCardFeatures() {
        let selectedTemplate = null;
        let isModalOpen = false;

        // 顯示說明 Modal
        christmasBtn.addEventListener('click', () => {
            // 防止重複點擊
            if (isModalOpen) return;
            isModalOpen = true;

            document.getElementById('cardInstructions').textContent = cardInstructions;
            cardModal.style.display = 'block';
            // 使用 requestAnimationFrame 確保 DOM 更新後再顯示
            requestAnimationFrame(() => {
                cardModal.classList.add('show');
            });
            selectedTemplate = null;
            document.querySelector('.understand-btn').disabled = true;
            document.querySelectorAll('.card-template').forEach(template => {
                template.classList.remove('selected');
            });
        });

        // 添加 Modal 關閉按鈕事件
        document.querySelector('.modal-close-btn').addEventListener('click', () => {
            cardModal.classList.remove('show');
            setTimeout(() => {
                cardModal.style.display = 'none';
                isModalOpen = false;
            }, 200);
        });

        // 點擊 modal 外部區域關閉
        cardModal.addEventListener('click', (e) => {
            if (e.target === cardModal) {
                cardModal.classList.remove('show');
                setTimeout(() => {
                    cardModal.style.display = 'none';
                    isModalOpen = false;
                }, 200);
            }
        });

        // 處理模板選擇
        document.querySelectorAll('.card-template').forEach(template => {
            template.addEventListener('click', () => {
                document.querySelectorAll('.card-template').forEach(t => {
                    t.classList.remove('selected');
                });
                template.classList.add('selected');
                selectedTemplate = template.dataset.name;
                document.querySelector('.understand-btn').disabled = false;
            });
        });

        // 開始選擇照片

        understandBtn.addEventListener('click', () => {
            if (!selectedTemplate) return;

            cardModal.classList.remove('show');
            setTimeout(() => {
                cardModal.style.display = 'none';
                isModalOpen = false;
                christmasBtn.style.display = 'none';
                cardToolbar.style.display = 'flex';
                isSelectingPhotos = true;
                selectedPhotos.clear();
                resetCardStatus(); // 重置狀態
                updatePhotoSelectUI();
                document.body.classList.add('card-selecting-mode');
            }, 200);
        });

        // 關閉選擇模式
        closeCardBtn.addEventListener('click', () => {
            exitPhotoSelectMode();
        });

        // 修改提交照片的部分
        cardStatusBtn.addEventListener('click', async () => {
            if (selectedPhotos.size >= 1 && selectedPhotos.size <= 10) {
                try {
                    const profile = await liff.getProfile();
                    const selectedPhotosData = Array.from(selectedPhotos.values())
                        .sort((a, b) => a.order - b.order);

                    const requestData = {
                        activity: selectedTemplate,
                        userId: profile.userId,
                        photos: selectedPhotosData,
                        folder_ids: window.folderIdList || []
                    };

                    console.log('準備發送到伺服器的資料：', requestData);

                    // 顯示戲劇性過渡動畫
                    const overlay = document.querySelector('.dramatic-overlay');
                    const message = overlay.querySelector('.dramatic-message');
                    overlay.style.display = 'flex';  // 先設置為 flex
                    requestAnimationFrame(() => {    // 使用 requestAnimationFrame 確保 DOM 更新
                        // 第一個訊息動畫
                        setTimeout(() => {
                            overlay.classList.add('show');
                            message.textContent = '卡片開始製作囉...';
                            setTimeout(() => {
                                message.style.opacity = '1';
                                message.style.transform = 'scale(1)';
                            }, 100);
                        }, 100);
                    });

                    const response = await fetch('https://student.wentzao.com/create_card', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify(requestData)
                    });

                    if (response.ok) {
                        const result = await response.json();
                        // alert(`https://student.wentzao.com/static/giftcards/${result.activity}/materials/cardcover.png`);
                        // 第二個訊息動畫
                        setTimeout(() => {
                            message.style.opacity = '0';
                            message.style.transform = 'scale(0.8)';
                            setTimeout(() => {
                                message.textContent = '開啟卡片！';
                                message.classList.add('sparkle');
                                message.style.opacity = '1';
                                message.style.transform = 'scale(1)';
                            }, 500);
                        }, 2000);

                        // 使用LIFF直接傳送到聊天室
                        if (liff.isInClient()) {
                            await liff.sendMessages([{
                                "type": "flex",
                                "altText": "賀卡",
                                "contents": {
                                    "type": "bubble",
                                    "body": {
                                        "type": "box",
                                        "layout": "vertical",
                                        "contents": [
                                            {
                                                "type": "image",
                                                "url": `https://student.wentzao.com/static/giftcards/${result.activity}/materials/cardcover.png`,
                                                "size": "full",
                                                "aspectMode": "cover",
                                                "aspectRatio": "2:3",
                                                "gravity": "top",
                                                "animated": true
                                            }
                                        ],
                                        "paddingAll": "0px",
                                        "action": {
                                            "type": "uri",
                                            "label": "action",
                                            "uri": `https://liff.line.me/1660786685-5361X8d7?activity=${result.activity}&card_id=${result.card_id}`,
                                            "altUri": {
                                                "desktop": `https://liff.line.me/1660786685-5361X8d7?activity=${result.activity}&card_id=${result.card_id}`
                                            }
                                        }
                                    }
                                }
                            }]);
                        }

                        // 延遲後關閉動畫並跳轉
                        setTimeout(() => {
                            message.style.opacity = '0';
                            message.style.transform = 'scale(0.8)';
                            setTimeout(() => {
                                overlay.classList.remove('show');
                                setTimeout(() => {
                                    overlay.style.display = 'none';
                                    message.classList.remove('sparkle');
                                    exitPhotoSelectMode();

                                    // 建立URL query參數
                                    const queryParams = `?activity=${result.activity}&card_id=${result.card_id}`;
                                    // 檢查是否在LIFF環境
                                    if (liff.isInClient()) {
                                        // 在LIFF環境中開啟新的LIFF URL
                                        liff.openWindow({
                                            url: `https://liff.line.me/1660786685-5361X8d7${queryParams}`,
                                            external: false
                                        });
                                    } else {
                                        // 非LIFF環境直接開啟網頁
                                        window.open(`https://student.wentzao.com/get_card${queryParams}`, '_blank');
                                    }
                                }, 800);
                            }, 500);
                        }, 3500);

                    } else {
                        throw new Error('伺服器回應錯誤');
                    }
                } catch (error) {
                    console.error('製作卡片失敗:', error);
                    showNotification('製作卡片失敗，請稍後再試');
                }
            }
        });
    }

    // 更新照片選擇UI
    function updatePhotoSelectUI() {
        if (!isSelectingPhotos) return;

        // 添加多選模式的 class
        cardToolbar.classList.add('selecting');

        const photoItems = document.querySelectorAll('.photo-item');
        photoItems.forEach(item => {
            const img = item.querySelector('img');
            const photoId = img.getAttribute('data-photoId');
            const photoType = img.getAttribute('data-type') || 'photo';
            const isVideo = photoType === 'video' || item.classList.contains('video');

            // 如果是影片，設置半透明效果並跳過添加選擇圈
            if (isVideo) {
                item.style.opacity = '0.4'; // 設置半透明
                // 確保影片沒有選擇圈
                let existingCircle = item.querySelector('.photo-select-circle');
                if (existingCircle) {
                    existingCircle.style.display = 'none';
                }

                // 移除原有的點擊事件並添加提示
                item.onclick = (e) => {
                    e.stopPropagation();
                    showNotification('影片無法添加到賀卡');
                };

                return; // 跳過後續處理
            } else {
                // 重置非影片項目的透明度
                item.style.opacity = '1';
            }

            let selectCircle = item.querySelector('.photo-select-circle');
            if (!selectCircle) {
                selectCircle = document.createElement('div');
                selectCircle.className = 'photo-select-circle';
                item.appendChild(selectCircle);
            }
            selectCircle.style.display = 'block';

            // 更新選中狀態和顯示順序
            const selectedPhoto = selectedPhotos.get(photoId);

            if (selectedPhoto) {
                selectCircle.classList.add('selected');
                item.classList.add('selected'); // 添加選中的邊框效果
                // 顯示選擇順序
                selectCircle.textContent = selectedPhoto.order;
                selectCircle.style.display = 'flex';
            } else {
                selectCircle.classList.remove('selected');
                item.classList.remove('selected'); // 移除選中的邊框效果
                selectCircle.textContent = '';
                selectCircle.style.display = 'flex';
            }

            // 移除原有的點擊事件
            item.onclick = null;
            selectCircle.onclick = null;

            // 添加新的點擊事件處理
            selectCircle.onclick = (e) => {
                e.stopPropagation();
                togglePhotoSelection(photoId, img.getAttribute('data-full'), img.getAttribute('data-name'), photoType);
            };

            item.onclick = (e) => {
                if (e.target !== selectCircle) {
                    const photoGrid = item.closest('.photo-grid');
                    const photoItems = Array.from(photoGrid.querySelectorAll('.photo-item'));
                    const photoIndex = photoItems.indexOf(item);

                    let photoList;
                    if (document.body.classList.contains('favorites-view-active')) {
                        photoList = [];
                        allFolders.forEach(folder => {
                            folder.photos.forEach(photo => {
                                if (favoritePhotos.has(photo.photoId)) {
                                    photoList.push(photo);
                                }
                            });
                        });
                        photoList.sort((a, b) => new Date(b.date) - new Date(a.date));
                    } else {
                        photoList = allFolders[currentMonthIndex].photos;
                    }

                    openFullscreen(photoList, photoIndex);
                }
            };
        });
    }

    // 切換照片選擇狀態
    function togglePhotoSelection(photoId, photoUrl, photoName, photoType) {
        // 如果是影片，不允許加入賀卡
        if (photoType === 'video') {
            showNotification('影片不能添加到賀卡');
            return;
        }

        if (selectedPhotos.has(photoId)) {
            selectedPhotos.delete(photoId);
            // 當移除照片時，需要重新計算所有照片的順序
            const newSelectedPhotos = new Map();
            let order = 1;
            Array.from(selectedPhotos.entries()).forEach(([id, data]) => {
                newSelectedPhotos.set(id, {
                    ...data,
                    order: order++
                });
            });
            selectedPhotos = newSelectedPhotos;
        } else if (selectedPhotos.size < 10) {
            // 新增照片時，添加順序號
            selectedPhotos.set(photoId, {
                photoId: photoId,
                url: photoUrl,
                name: photoName,
                order: selectedPhotos.size + 1
            });
        }
        updatePhotoSelectUI();
        updateCardStatus();
    }

    // 更新賀卡狀態
    function updateCardStatus() {
        const count = selectedPhotos.size;
        const magicText = cardStatusBtn.querySelector('.magic-text');

        if (count < 1) {
            cardStatusBtn.innerHTML = `您還需要選擇<span class="magic-text">1張照片</span>`;
            cardStatusBtn.classList.remove('ready');
        } else if (count >= 1 && count < 10) {
            cardStatusBtn.innerHTML = `已選擇${count}張照片，<span class="magic-text">開始製作</span>`;
            cardStatusBtn.classList.add('ready');
        } else if (count == 10) {
            cardStatusBtn.innerHTML = `<span class="magic-text">開始製作</span>`;
            cardStatusBtn.classList.add('ready');
        }
    }

    // 重置卡片狀態
    function resetCardStatus() {
        cardStatusBtn.innerHTML = `您還需要選擇<span class="magic-text">1張照片</span>`;
        cardStatusBtn.classList.remove('ready');
    }

    // 退出照片選擇模式
    function exitPhotoSelectMode() {
        isSelectingPhotos = false;
        selectedPhotos.clear();
        christmasBtn.style.display = 'block';
        cardToolbar.style.display = 'none';

        // 移除多選模式的 class
        cardToolbar.classList.remove('selecting');
        document.body.classList.remove('card-selecting-mode');

        // 重置卡片狀態按鈕
        resetCardStatus();

        // 移除所有選擇圈和選中效果，並重置透明度
        document.querySelectorAll('.photo-item').forEach(item => {
            // 重置透明度
            item.style.opacity = '1';

            const circle = item.querySelector('.photo-select-circle');
            if (circle) {
                circle.style.display = 'none';
                circle.classList.remove('selected');
                circle.textContent = ''; // 清空數字
            }
            item.classList.remove('selected'); // 移除選中的邊框效果
        });

        // 恢復原有的點擊事件
        document.querySelectorAll('.photo-item').forEach(item => {
            const photoGrid = item.closest('.photo-grid');
            const photoItems = Array.from(photoGrid.querySelectorAll('.photo-item'));
            const index = photoItems.indexOf(item);
            const photoList = allFolders[currentMonthIndex].photos;
            item.onclick = () => openFullscreen(photoList, index);
        });
    }

    // 日期選擇器功能
    function initializeDatePicker() {
        const currentDateElement = document.querySelector('.current-date');
        const datePicker = document.querySelector('.date-picker');
        const datePickerYears = document.querySelector('.date-picker-years');
        const datePickerMonths = document.querySelector('.date-picker-months');
        let selectedYear = null;

        // 點擊當前日期顯示選擇器
        currentDateElement.addEventListener('click', (e) => {
            // 確保點擊的是日期文字，不是選擇器本身
            if (e.target.closest('.current-date')) {
                e.stopPropagation();
                if (!document.body.classList.contains('favorites-view-active')) {
                    toggleDatePicker();
                }
            }
        });

        // 點擊其他地方關閉選擇器
        document.addEventListener('click', (e) => {
            if (datePicker && currentDateElement && !datePicker.contains(e.target) && !currentDateElement.contains(e.target)) {
                // 檢查是否點擊了照片或照片相關元素
                const isPhotoElement = e.target.closest('.photo-item') ||
                    e.target.closest('.photo-grid') ||
                    e.target.closest('#albums-container');

                if (datePicker.classList.contains('show')) {
                    e.preventDefault();
                    e.stopPropagation();
                    hideDatePicker();

                    // 如果點擊的是照片相關元素，阻止事件繼續傳播
                    if (isPhotoElement) {
                        return false;
                    }
                }
            }
        }, true); // 使用捕获階段

        function toggleDatePicker() {
            if (datePicker.classList.contains('show')) {
                hideDatePicker();
            } else {
                showDatePicker();
            }
        }

        function showDatePicker() {
            populateDatePicker();
            datePicker.classList.remove('hide');
            // 使用 requestAnimationFrame 確保 DOM 更新後再顯示
            requestAnimationFrame(() => {
                datePicker.classList.add('show');
            });
        }

        function hideDatePicker() {
            datePicker.classList.remove('show');
            datePicker.classList.add('hide');
        }

        function populateDatePicker() {
            // 從所有相簿中提取年份和月份資料
            const availableDates = new Map();
            allFolders.forEach((folder, index) => {
                const [year, month] = folder.date.split('-');
                if (!availableDates.has(year)) {
                    availableDates.set(year, new Set());
                }
                availableDates.get(year).add({
                    month: parseInt(month),
                    index: index,
                    date: folder.date
                });
            });

            // 生成年份按鈕
            datePickerYears.innerHTML = '';
            const sortedYears = Array.from(availableDates.keys()).sort((a, b) => b - a);

            sortedYears.forEach(year => {
                const yearBtn = document.createElement('button');
                yearBtn.className = 'date-picker-year';
                yearBtn.textContent = year + '年';
                yearBtn.dataset.year = year;

                // 檢查是否為當前年份
                const currentFolder = allFolders[currentMonthIndex];
                if (currentFolder) {
                    const currentYear = currentFolder.date.split('-')[0];
                    if (year === currentYear) {
                        yearBtn.classList.add('selected');
                        selectedYear = year;
                        populateMonths(year, availableDates.get(year));
                    }
                }

                yearBtn.addEventListener('click', () => {
                    // 更新年份選擇
                    document.querySelectorAll('.date-picker-year').forEach(btn => {
                        btn.classList.remove('selected');
                    });
                    yearBtn.classList.add('selected');
                    selectedYear = year;
                    populateMonths(year, availableDates.get(year));
                });

                datePickerYears.appendChild(yearBtn);
            });
        }

        function populateMonths(year, monthsData) {
            datePickerMonths.innerHTML = '';
            const monthNames = ['1月', '2月', '3月', '4月', '5月', '6月',
                '7月', '8月', '9月', '10月', '11月', '12月'];

            const availableMonths = Array.from(monthsData);
            const currentFolder = allFolders[currentMonthIndex];
            const currentYear = currentFolder ? currentFolder.date.split('-')[0] : '';
            const currentMonth = currentFolder ? currentFolder.date.split('-')[1] : '';

            const currentDate = new Date();
            const currentRealYear = currentDate.getFullYear();
            const currentRealMonth = currentDate.getMonth() + 1;

            // 按月份從新到舊排列（12月到1月）
            for (let month = 12; month >= 1; month--) {
                const monthData = availableMonths.find(m => m.month === month);

                // 檢查是否為未來月份
                const isFutureMonth = (parseInt(year) > currentRealYear) ||
                    (parseInt(year) === currentRealYear && month > currentRealMonth);

                // 只顯示有資料的月份，且不是未來月份
                if (monthData && !isFutureMonth) {
                    const monthBtn = document.createElement('button');
                    monthBtn.className = 'date-picker-month';
                    monthBtn.textContent = monthNames[month - 1];
                    monthBtn.dataset.index = monthData.index;
                    monthBtn.dataset.date = monthData.date;

                    // 檢查是否為當前月份
                    if (year === currentYear && month === parseInt(currentMonth)) {
                        monthBtn.classList.add('selected');
                    }

                    monthBtn.addEventListener('click', () => {
                        // 立即隱藏選擇器
                        hideDatePicker();

                        // 跳轉到選擇的月份
                        currentMonthIndex = monthData.index;
                        displayAlbums();
                        updateDateDisplay();

                        // 滾動到頂部
                        window.scrollTo({
                            top: 0,
                            behavior: 'smooth'
                        });
                    });

                    datePickerMonths.appendChild(monthBtn);
                }
            }
        }
    }

    // 初始化電子賀卡功能
    // Teacher version: skip card features if elements don't exist
    if (christmasBtn) {
        initializeCardFeatures();
    }

    // 初始化日期選擇器功能
    initializeDatePicker();

    /**
     * 全螢幕手勢處理
     */
    function handleFullscreenTouchStart(e) {
        if (e.touches.length > 1) return; // 忽略多點觸控
        fsTouchStartY = e.touches[0].clientY;
        fsTouchStartX = e.touches[0].clientX;
        fsIsDragging = true;
        fsSwipeDirection = null; // 重置方向
        const swiperContainer = document.querySelector('.swiper');
        if (swiperContainer) {
            swiperContainer.style.transition = 'none';
        }
    }

    function handleFullscreenTouchMove(e) {
        if (!fsIsDragging || e.touches.length > 1) return;

        const deltaY = e.touches[0].clientY - fsTouchStartY;
        const deltaX = e.touches[0].clientX - fsTouchStartX;

        // 判斷滑動方向
        if (!fsSwipeDirection) {
            if (Math.abs(deltaY) > 10 || Math.abs(deltaX) > 10) {
                fsSwipeDirection = Math.abs(deltaY) > Math.abs(deltaX) ? 'vertical' : 'horizontal';
            }
        }

        if (fsSwipeDirection === 'vertical') {
            e.preventDefault();
            e.stopPropagation();

            // 只處理向下滑動
            if (deltaY > 0) {
                const swiperContainer = document.querySelector('.swiper');
                const overlay = document.querySelector('.fullscreen-overlay');
                const dragRatio = deltaY / window.innerHeight;

                if (swiperContainer) {
                    swiperContainer.style.transform = `translateY(${deltaY}px)`;
                }
                if (overlay) {
                    overlay.style.backgroundColor = `rgba(0, 0, 0, ${0.9 - dragRatio * 0.5})`;
                }
            }
        }
        // 如果是水平滑動，則不做任何事，讓 Swiper 處理
    }

    function handleFullscreenTouchEnd(e) {
        if (!fsIsDragging) return;
        fsIsDragging = false;

        if (fsSwipeDirection === 'vertical') {
            const deltaY = e.changedTouches[0].clientY - fsTouchStartY;
            const threshold = window.innerHeight / 7;

            if (deltaY > threshold) {
                closeFullscreen();
            } else {
                // 動畫彈回原位
                const swiperContainer = document.querySelector('.swiper');
                const overlay = document.querySelector('.fullscreen-overlay');
                if (swiperContainer) {
                    swiperContainer.style.transition = 'transform 0.3s ease';
                    swiperContainer.style.transform = 'translateY(0px)';
                }
                if (overlay) {
                    overlay.style.transition = 'background-color 0.3s ease';
                    overlay.style.backgroundColor = 'rgba(0, 0, 0, 0.9)';
                }

                // 動畫結束後清除樣式
                setTimeout(() => {
                    if (swiperContainer) swiperContainer.style.transition = '';
                    if (overlay) overlay.style.transition = '';
                }, 300);
            }
        }
        fsSwipeDirection = null; // 為下次觸控重置
    }

    /**
     * 從後端獲取 API key，帶有重試機制
     * @param {number} retryCount - 重試次數
     * @param {number} delay - 重試延遲時間 (毫秒)
     * @returns {Promise<object>} - 包含 API key 和代理伺服器 URL 的物件
     */
    async function getApiKeyWithRetry(retryCount = 3, delay = 1000) {
        let lastError = null;

        // 使用靜態變數來追蹤最近使用的 API key 及其使用時間
        if (!getApiKeyWithRetry.lastUsedKeys) {
            getApiKeyWithRetry.lastUsedKeys = new Map();
        }

        // 定義 API key 冷卻期 (毫秒)
        const cooldownPeriod = 2000;

        for (let i = 0; i < retryCount; i++) {
            try {
                console.log(`嘗試獲取 API key (第 ${i + 1} 次嘗試)`);
                const response = await fetch('/api/teacher/video_api_key', { method: 'POST' });

                if (!response.ok) {
                    throw new Error(`API key 請求失敗: ${response.status}`);
                }

                const data = await response.json();

                if (data.status === 'success' && data.api_key) {
                    const apiKey = data.api_key;
                    const proxyUrl = data.proxy_url; // 獲取代理伺服器 URL
                    const now = Date.now();

                    // 檢查這個 API key 是否在冷卻期內
                    const lastUsedTime = getApiKeyWithRetry.lastUsedKeys.get(apiKey);
                    if (lastUsedTime && (now - lastUsedTime < cooldownPeriod)) {
                        console.log(`API key ${apiKey.substring(0, 5)}... 在冷卻期內，延遲 ${cooldownPeriod}ms 後重試`);
                        // 如果 key 在冷卻期內，等待一段時間後重試
                        await new Promise(resolve => setTimeout(resolve, cooldownPeriod));
                        continue;
                    }

                    // 更新最近使用的 API key 及其時間
                    getApiKeyWithRetry.lastUsedKeys.set(apiKey, now);

                    // 清理過期的 key 記錄 (超過 5 分鐘的)
                    const expiryTime = 5 * 60 * 1000; // 5 分鐘
                    for (const [key, time] of getApiKeyWithRetry.lastUsedKeys.entries()) {
                        if (now - time > expiryTime) {
                            getApiKeyWithRetry.lastUsedKeys.delete(key);
                        }
                    }

                    console.log(`成功獲取 API key: ${apiKey.substring(0, 5)}...`);
                    if (proxyUrl) {
                        console.log(`使用代理伺服器: ${proxyUrl}`);
                    } else {
                        console.log(`使用直接連線`);
                    }

                    return { apiKey, proxyUrl };
                } else {
                    throw new Error('無效的 API key 回應');
                }
            } catch (error) {
                console.warn(`獲取 API key 失敗 (嘗試 ${i + 1}/${retryCount}): ${error.message}`);
                lastError = error;

                // 最後一次嘗試失敗時不需要等待
                if (i < retryCount - 1) {
                    await new Promise(resolve => setTimeout(resolve, delay));
                    // 每次重試增加延遲時間
                    delay *= 1.5;
                }
            }
        }

        // 所有重試都失敗了
        throw lastError || new Error('無法獲取 API key');
    }
});